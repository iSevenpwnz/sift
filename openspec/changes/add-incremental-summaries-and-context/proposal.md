# Change: Incremental chat summaries, context-aware categorization, important people

## Why
The daily digest calls AI at read-time on all messages, which times out on OpenRouter free tier (Qwen 3.6, 20s timeout). Categorization lacks context -- it sees isolated messages, not the day's discussion. There's no way to mark important people for priority boosting.

## What Changes
- New `ChatDailySummary` model: stores per-chat per-day rolling summaries, updated incrementally after each batch
- `process_messages()` gains a post-categorization step: update summaries for affected chats via small AI calls
- `build_digest()` reads pre-built summaries from DB instead of calling AI (eliminates timeout problem)
- Categorization prompt receives chat daily summary as context for better accuracy
- `UserSettings.important_people` JSONB field added, injected into categorization prompt
- New prompt file `prompts/update_summary.txt` for incremental summary updates

## Impact
- Affected code: `src/app/db/models.py`, `src/app/processors/pipeline.py`, `src/app/processors/ai_provider.py`, `src/app/scheduler/jobs.py`, `prompts/categorize.txt`, `prompts/update_summary.txt` (new)
- New migration: adds `chat_daily_summaries` table + `important_people` column to `user_settings`
- No breaking changes to existing functionality
