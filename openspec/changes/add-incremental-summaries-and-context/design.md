# Design: Incremental Summaries Data Flow

## Before (current)

```
Message arrives -> persist_raw -> process_messages (L1 + L2 AI) -> save + notify
                                                                         |
Digest request (manual or scheduled) -> load ALL messages -> AI call (big) -> render
                                                              ^ TIMES OUT on free tier
```

## After (proposed)

```
Message arrives -> persist_raw -> process_messages:
                                    1. L1 filter
                                    2. Load chat_context + important_people from DB
                                    3. L2 AI categorize (with context + people in prompt)
                                    4. Save AI results
                                    5. update_chat_summaries (small AI call per chat)
                                    6. Create tasks + notify

Digest request -> load ChatDailySummary rows from DB -> render (ZERO AI calls)
                  ^ if no summaries: fallback to _summarize_groups()
```

## Token budget per AI call

### Categorization (existing, modified)
- System prompt: ~400 tokens (categorize.txt)
- important_people section: ~50 tokens (5 people)
- Per message: ~50 tokens (id, chat, sender, text:100chars, chat_context:300chars)
- Batch of 10: ~500 tokens user content
- **Total: ~950 tokens input** (was ~550 without context -- acceptable increase)

### Summary update (new)
- System prompt: ~150 tokens (update_summary.txt)
- current_summary: ~100 tokens (max 300 chars)
- new_messages: 5 messages * 30 tokens = ~150 tokens
- **Total: ~400 tokens input, ~100 tokens output**
- Per batch of 10 messages from 3 chats: 3 calls * 400 = 1200 tokens total

### Digest (modified)
- **Before: 1 large call ~2000+ tokens, often timing out**
- **After: 0 AI calls, just DB reads**

## Failure modes

| Scenario | Impact | Recovery |
|----------|--------|----------|
| Summary AI fails | Stale summary for that chat | Next batch retries naturally |
| Summary AI returns junk | Bad summary text | Overwritten by next batch |
| DB upsert fails | Lost summary | Next batch creates fresh |
| All AI fails for batch | Messages go to pending_ai | retry_pending_ai job picks up in 5min |
| No summaries in DB for digest | Empty summaries dict | Falls back to _summarize_groups() |

## Migration safety

Both changes (new table + new column) are additive:
- `chat_daily_summaries` is a new table -- no impact on existing data
- `important_people` JSONB with default=list -- nullable not needed, existing rows get `[]`
- No data migration needed
- Rollback: drop table + drop column
