# Research: Incremental Chat Summaries + Context + Important People

## Existing Patterns

### Data layer
- `src/app/db/models.py`: 4 models (Message, Task, Reminder, UserSettings), all use `mapped_column`, SQLAlchemy 2.0 declarative style.
- `UserSettings` already has JSONB fields (`monitored_chats`, `ignored_chats`, `quiet_hours`) -- adding `important_people` as JSONB is consistent.
- Alembic async migrations at `src/app/db/migrations/`, single existing migration `a7af7459d5b1`.

### Pipeline
- `src/app/processors/pipeline.py:process_messages()` -- batch processing: L1 filter -> L2 AI categorize -> save -> notify. **This is where summary updates should go** (after AI categorization saves, before notifications).
- AI provider: `OpenAICompatibleProvider.categorize()` -- takes list of dicts, returns structured JSON. System prompt loaded from file with `{current_datetime}` placeholder.
- Already uses `pg_insert().on_conflict_do_update()` in `persist_raw()` -- same upsert pattern for ChatDailySummary.

### Digest
- `src/app/scheduler/jobs.py:build_digest()` -- loads all messages for the day, groups by chat, calls `_summarize_groups()` which makes a LIVE AI call with 20s timeout. **This is the bottleneck** -- free tier Qwen times out on large payloads.
- `_summarize_groups()` takes top 10 chats, max 8 messages each, max 150 chars per message. Still too much for free tier.
- Digest has a 5-min in-memory cache (`_digest_cache`).

### Prompt structure
- `prompts/categorize.txt` -- system prompt with owner context hardcoded (team names, interests, work description).
- `prompts/digest_summary.txt` -- simple "summarize each chat" prompt.
- No template for incremental summary yet -- need new `prompts/update_summary.txt`.

## External Research

### Incremental summarization (LLM)
- Recursive summarization pattern: "old summary + new messages -> updated summary" is well-established (arxiv.org/html/2308.15022v3).
- Key insight: keep summaries short (2-4 sentences) to avoid error amplification in cascading compressions.
- For our case: one small AI call per chat per batch (~100-200 tokens input) vs. one large call at digest time (~2000+ tokens).

### SQLAlchemy 2.0 async upsert
- `pg_insert().on_conflict_do_update()` is the standard pattern, already used in codebase (`persist_raw()`).
- Using `returning()` with upsert requires `Populate Existing` option for session-cached objects.

## Reuse Opportunities

1. **Upsert pattern**: Copy from `persist_raw()` in pipeline.py -- same `pg_insert().on_conflict_do_update()`.
2. **AI provider**: Reuse `get_primary_provider().client.chat.completions.create()` directly (same as `_summarize_groups()` does).
3. **Prompt loading**: Same `Path(__file__) / "prompts"` pattern already used in `ai_provider.py` and `jobs.py`.
4. **UserSettings JSONB**: Same pattern as `quiet_hours` -- JSONB with default dict/list.

## Decision

### Why incremental summaries:
- Current `_summarize_groups()` makes one large AI call at digest time with 20s timeout -- fails on free tier.
- Incremental: many small calls (1 per chat per batch, ~100 tokens each) spread across the day.
- At digest time: just read from DB, zero AI calls. Instant digest.

### Why put summary update in pipeline, not scheduler:
- Pipeline already processes messages in batches of 10.
- After categorization, we know which chats had messages. Update only those.
- If AI call fails, summary is stale but not broken (old summary still valid).

### Why JSONB for important_people (not separate table):
- Single user, max 10-20 people. No querying needed.
- Same pattern as `ignored_chats`, `quiet_hours`.
- Zero new tables for this feature.
