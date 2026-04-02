# Tasks: Incremental Summaries + Context + Important People

## Business Rules
- BR-SUM-001: Each chat has at most ONE summary per date (unique on chat_name + date)
- BR-SUM-002: Summary update = "old summary + new messages -> new summary" via small AI call (~200 tokens input max)
- BR-SUM-003: If summary AI call fails, keep old summary (stale is better than empty)
- BR-SUM-004: build_digest reads summaries from DB, makes ZERO AI calls
- BR-CTX-001: Categorization AI receives current chat_daily_summary as context (if exists)
- BR-CTX-002: Context is read-only -- categorization does NOT update the summary
- BR-PPL-001: important_people format: [{"name": "...", "relation": "...", "priority": "always_high|boost|normal"}]
- BR-PPL-002: important_people injected into categorization prompt so AI can boost priority for key people

## 1. DB Model + Migration

- [ ] 1.1 Add `ChatDailySummary` model to `src/app/db/models.py`

```python
class ChatDailySummary(Base):
    __tablename__ = "chat_daily_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_name: Mapped[str] = mapped_column(String(255), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_chat_date", "chat_name", "date", unique=True),
    )
```

- [ ] 1.2 Add `important_people` JSONB to `UserSettings`

```python
# In UserSettings class, add:
important_people: Mapped[dict] = mapped_column(JSONB, default=list)
```

- [ ] 1.3 Generate and review Alembic migration

```bash
alembic revision --autogenerate -m "add_chat_daily_summaries_and_important_people"
```

Verify migration has:
- `create_table('chat_daily_summaries', ...)` with unique index on (chat_name, date)
- `add_column('user_settings', 'important_people', JSONB, default=[])`

## 2. Incremental Summary Update (core logic)

- [ ] 2.1 Create `prompts/update_summary.txt`

```
You update a running daily summary for a Telegram chat.

## Input
- chat_name: the chat/channel name
- current_summary: the existing summary for today (may be empty if first batch)
- new_messages: new messages that just arrived

## Rules
1. Write in UKRAINIAN only.
2. Merge new_messages into current_summary. Keep it 2-4 sentences max.
3. Preserve key facts from current_summary. Add new developments.
4. If someone asked Mykhailo to do something -- highlight: "Тебе просили: ..."
5. If decisions were made -- state them: "Вирішили: ..."
6. If the chat is just noise/banter, say: "Загальне обговорення, нічого конкретного."
7. Max 300 characters total.

## Output
Return ONLY the updated summary text. No JSON, no explanation, just plain text.
```

- [ ] 2.2 Add `update_chat_summaries()` function to `src/app/processors/pipeline.py`

Pseudocode:
```python
async def update_chat_summaries(messages: list[Message]) -> None:
    """Update daily summaries for chats that had messages in this batch."""
    today = datetime.now(USER_TZ).date()

    # Group messages by chat
    chat_msgs: dict[str, list[Message]] = defaultdict(list)
    for msg in messages:
        if msg.source_chat and msg.category != "noise":
            chat_msgs[msg.source_chat].append(msg)

    if not chat_msgs:
        return

    # Load existing summaries for today
    async with async_session() as session:
        result = await session.execute(
            select(ChatDailySummary)
            .where(ChatDailySummary.date == today)
            .where(ChatDailySummary.chat_name.in_(list(chat_msgs.keys())))
        )
        existing = {s.chat_name: s for s in result.scalars().all()}

    # Load prompt template once
    prompt_template = Path(...) / "prompts" / "update_summary.txt"
    system_prompt = prompt_template.read_text()

    provider = get_primary_provider()

    for chat_name, msgs in chat_msgs.items():
        current = existing.get(chat_name)
        current_summary = current.summary_text if current else ""
        current_count = current.message_count if current else 0

        # Build compact input
        new_messages_text = "\n".join(
            f"{m.sender or ''}: {m.content[:100]}" for m in msgs[:5]
        )

        user_content = (
            f"chat_name: {chat_name}\n"
            f"current_summary: {current_summary}\n"
            f"new_messages:\n{new_messages_text}"
        )

        try:
            response = await asyncio.wait_for(
                provider.client.chat.completions.create(
                    model=provider.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.3,
                    max_tokens=200,
                ),
                timeout=10,
            )
            new_summary = response.choices[0].message.content or current_summary
        except Exception:
            logger.warning(f"Failed to update summary for {chat_name}, keeping old")
            new_summary = current_summary  # BR-SUM-003: keep old on failure

        # Upsert
        async with async_session() as session:
            stmt = (
                pg_insert(ChatDailySummary)
                .values(
                    chat_name=chat_name,
                    date=today,
                    summary_text=new_summary.strip(),
                    message_count=current_count + len(msgs),
                    last_updated=func.now(),
                )
                .on_conflict_do_update(
                    index_elements=["chat_name", "date"],
                    set_={
                        "summary_text": new_summary.strip(),
                        "message_count": current_count + len(msgs),
                        "last_updated": func.now(),
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()
```

- [ ] 2.3 Call `update_chat_summaries()` from `process_messages()` after categorization saves

Location: `src/app/processors/pipeline.py`, after line 213 (after `await session.commit()` that saves AI results), before task creation loop.

```python
    # After AI results saved, update incremental summaries
    categorized_msgs = [m for m in to_ai if m.id in result_map]
    try:
        await update_chat_summaries(categorized_msgs)
    except Exception:
        logger.warning("Failed to update chat summaries, continuing")
```

Important: wrap in try/except so summary failure never blocks the main pipeline.

## 3. Modify build_digest to use pre-built summaries

- [ ] 3.1 Replace `_summarize_groups()` call in `build_digest()` with DB read

In `src/app/scheduler/jobs.py`, around line 203:

**Before:**
```python
summaries = await _summarize_groups(chat_groups)
```

**After:**
```python
# Read pre-built summaries from DB (zero AI calls)
from src.app.db.models import ChatDailySummary
async with async_session() as session:
    result = await session.execute(
        select(ChatDailySummary)
        .where(ChatDailySummary.date == target_date)
    )
    summaries = {s.chat_name: s.summary_text for s in result.scalars().all() if s.summary_text}
```

- [ ] 3.2 Keep `_summarize_groups()` as fallback for historical dates (no summaries in DB)

```python
if not summaries:
    # Fallback for historical dates before incremental summaries existed
    summaries = await _summarize_groups(chat_groups)
```

- [ ] 3.3 Delete `_digest_cache` invalidation from `process_messages()` (line 232-233)

The `_digest_cache.clear()` call is no longer needed since summaries come from DB. But keep the cache mechanism in `build_digest()` itself for repeat-call efficiency (it caches rendered HTML, not AI results).

Actually -- KEEP the cache invalidation. It's still useful because the rendered digest HTML should refresh when new messages arrive. The cache is cheap.

## 4. Context-aware categorization

- [ ] 4.1 Load chat summaries before AI categorization in `process_messages()`

In `src/app/processors/pipeline.py`, before the AI categorization call (around line 157):

```python
# Load existing daily summaries for context
from src.app.db.models import ChatDailySummary
today = datetime.now(USER_TZ).date()
chat_names = list(set(m.source_chat for m in to_ai if m.source_chat))
chat_context = {}
if chat_names:
    async with async_session() as session:
        result = await session.execute(
            select(ChatDailySummary)
            .where(ChatDailySummary.date == today)
            .where(ChatDailySummary.chat_name.in_(chat_names))
        )
        chat_context = {s.chat_name: s.summary_text for s in result.scalars().all() if s.summary_text}
```

- [ ] 4.2 Include context in AI input

Modify `ai_input` construction (line 157-166):

```python
ai_input = [
    {
        "id": msg.id,
        "chat": msg.source_chat or "",
        "sender": msg.sender or "",
        "text": msg.content,
        "reply_to": msg.reply_to_text,
        "type": msg.content_type,
        "chat_context": chat_context.get(msg.source_chat, ""),  # NEW
    }
    for msg in to_ai
]
```

- [ ] 4.3 Update `prompts/categorize.txt` to mention context field

Add after "## What you receive" section (after line 12):

```
If a "chat_context" field is provided, it contains a running summary of today's discussion in that chat.
Use it to understand what the conversation is about -- e.g., if someone says "модуль" and context mentions a SOLID discussion, they mean a code module, not a random word.
Do NOT repeat context in your topic -- just use it for better categorization.
```

## 5. Important people config

- [ ] 5.1 Load important_people in `process_messages()` alongside chat_context

```python
# Load user settings for important_people
from src.app.db.models import UserSettings
async with async_session() as session:
    result = await session.execute(
        select(UserSettings).where(UserSettings.telegram_user_id == settings.telegram_owner_id)
    )
    us = result.scalar_one_or_none()
    important_people = us.important_people if us and us.important_people else []
```

- [ ] 5.2 Add important_people to `_get_system_prompt()` in `ai_provider.py`

Change `_get_system_prompt()` to accept optional `important_people` param:

```python
def _get_system_prompt(important_people: list[dict] | None = None) -> str:
    template = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else "Categorize messages. Return JSON."
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = template.replace("{current_datetime}", now)

    if important_people:
        people_lines = []
        for p in important_people:
            people_lines.append(f"- {p['name']}: {p.get('relation', '')} (priority: {p.get('priority', 'normal')})")
        people_section = "\n## Important people\n" + "\n".join(people_lines)
        people_section += "\nIf a message is from or mentions an important person with priority 'always_high', set priority to 'high'."
        people_section += "\nIf priority is 'boost', lean towards 'high' when the message has any substance."
        prompt += "\n" + people_section

    return prompt
```

- [ ] 5.3 Thread important_people through to `categorize()`

Option A (simpler): Pass important_people to `_get_system_prompt()` at call site.

In `ai_provider.py`, change `categorize()` signature to accept `important_people`:

```python
async def categorize(self, messages: list[dict], important_people: list[dict] | None = None) -> list[dict]:
    user_content = json.dumps({"messages": messages}, ensure_ascii=False)
    response = await self.client.chat.completions.create(
        model=self.model,
        messages=[
            {"role": "system", "content": _get_system_prompt(important_people)},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    ...
```

Update `AIProvider` Protocol too:
```python
class AIProvider(Protocol):
    async def categorize(self, messages: list[dict], important_people: list[dict] | None = None) -> list[dict]: ...
```

Update both `OpenAICompatibleProvider` and `GeminiProvider`.

- [ ] 5.4 Pass important_people from `process_messages()` to `categorize()`

```python
results = await get_primary_provider().categorize(ai_input, important_people=important_people)
```

(Same for fallback call.)

- [ ] 5.5 Add `/people` command or settings screen for managing important_people (LOW PRIORITY -- can use DB/JSON for now)

Skip for v1. User can set via direct DB edit or future `/settings` extension.

## 6. Cleanup job for old summaries

- [ ] 6.1 Add summary cleanup to `cleanup_old_data()` in `src/app/scheduler/jobs.py`

```python
# After existing message/task cleanup:
from src.app.db.models import ChatDailySummary
summary_result = await session.execute(
    delete(ChatDailySummary).where(ChatDailySummary.date < cutoff.date())
)
summaries_deleted = summary_result.rowcount
logger.info(f"Cleanup: deleted {messages_deleted} messages, {tasks_deleted} tasks, {summaries_deleted} summaries")
```

## Edge Cases and Potential Bugs

### E1: Race condition on summary upsert
Multiple batches processing simultaneously could try to upsert the same chat+date.
**Mitigation:** `on_conflict_do_update` is atomic. Last write wins. message_count could be inaccurate by a few, but summary_text will be the latest version.
**Acceptable:** message_count is cosmetic, summary_text is best-effort anyway.

### E2: Summary AI returns garbage/JSON instead of plain text
`max_tokens=200` limits damage. But prompt says "Return ONLY the updated summary text."
**Mitigation:** Strip the response. If it starts with `{` or `[`, discard and keep old summary.

### E3: Chat name changes mid-day (Telegram allows this)
Summary keyed on `chat_name` string. If chat renames, old summary orphaned, new one starts fresh.
**Acceptable:** Rare, and the old summary gets cleaned up after 30 days.

### E4: First batch of the day -- no existing summary
`current_summary` is empty string. Prompt handles this: "may be empty if first batch."
AI just writes a fresh summary from new_messages.

### E5: Empty messages after L1 filter (all noise)
`update_chat_summaries` only processes messages where `category != "noise"`.
If all messages are noise, no summary update -- correct behavior.

### E6: important_people is None/empty on first run
`_get_system_prompt(None)` skips the section entirely. No change to existing behavior.

### E7: Categorization input grows with chat_context
Each message gets `chat_context` (max ~300 chars per the summary prompt limit).
With batch of 10 messages from 3 chats, adds ~900 chars. Total input stays under 2000 tokens.

### E8: Historical digests (before summaries existed)
`build_digest()` falls back to `_summarize_groups()` when no DB summaries found. Preserves existing behavior for `/history` command.

## Implementation Order

1. **1.1-1.3**: DB model + migration (everything depends on this)
2. **2.1**: Create update_summary prompt
3. **2.2-2.3**: Incremental summary update in pipeline
4. **3.1-3.3**: Modify build_digest to read from DB
5. **4.1-4.3**: Add context to categorization
6. **5.1-5.4**: Important people config
7. **6.1**: Cleanup job
8. Test end-to-end with a few messages

Rationale: DB first (blocking dependency), then core summary pipeline (biggest value -- fixes timeout), then enhancements (context, people) that build on summaries.
