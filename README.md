# Sift

**Personal AI hub — sifts signal from noise across your messaging channels.**

Sift passively monitors your Telegram chats, categorizes messages with AI, and sends you notifications about what actually matters. A Telegram bot serves as the single interface for everything.

## How it works

```
Telegram chats (Telethon) → AI categorization → Smart notifications → Daily digest
```

1. **Telethon** reads your chats passively (groups, DMs, channels)
2. **Two-level filter**: regex pre-filter + AI categorization with chain-of-thought reasoning
3. **Smart notifications** — grouped by chat, edit-in-place, expandable summaries
4. **Daily digest** — DMs, groups, channels separated, AI summaries, clickable links
5. **Reminders** — AI decides when to remind you (1h before meetings, etc.)

## Features

### Smart Agent
- **Chain-of-thought reasoning** — AI explains WHY it categorizes each message
- **Incremental summaries** — daily chat summaries built in real-time, not at digest time
- **Context-aware** — AI sees today's discussion when categorizing new messages
- **Smart reminders** — AI decides if/when to remind (meetings, deadlines, events)
- **Important people** — configurable priority (boss = always high)

### Notifications
- **Proactive** — bot messages you when something important happens
- **Grouped** — messages from same chat merged into one notification
- **Edit-in-place** — 3+ messages → notification replaced with AI summary
- **Quick reply** — reply directly from notification via Telethon
- **Quiet hours** — no notifications during configured hours
- **Mute** — `/mute 2` to silence for 2 hours

### Digest
- **Sections** — DMs → Groups → Channels, each with summaries
- **Expandable blockquotes** — compact view, tap to expand
- **Clickable links** — each news item links to original message
- **Navigation** — browse between days with inline buttons
- **Cached** — instant load, invalidates when new messages arrive

### Chat Management
- **Auto-discovery** — new chats detected → bot asks [Monitor] [Ignore]
- **Settings** — interactive inline menu with chat toggles, pagination
- **Timezone, digest time, quiet hours** — all configurable

### Tasks
- **Auto-extraction** — AI creates tasks from messages (meetings, deadlines)
- **Inline buttons** — [Done] [Snooze 1h] [Snooze 1d]
- **Deduplication** — no duplicate tasks for same topic
- **Snooze reminders** — bot re-notifies when snooze expires

### Other
- **`/catchup`** — streaming digest of ALL unread messages (DMs → groups → channels)
- **`/search`** — full-text search across all messages with links
- **`/history`** — digest for any date
- **`/status`** — system stats
- **`/week`** — this week's meetings and deadlines

## Bot commands

| Command | Description |
|---------|-------------|
| `/start` | Start bot, show persistent keyboard |
| `/summary` | Daily digest (or tap "Дайджест") |
| `/tasks` | Active tasks with action buttons |
| `/week` | This week's meetings and deadlines |
| `/catchup` | Streaming digest of all unread messages |
| `/search <query>` | Search all messages |
| `/history <date>` | Digest for a specific date |
| `/mute [hours]` | Mute notifications (default 1h) |
| `/unmute` | Unmute notifications |
| `/status` | System stats |
| `/settings` | Chat filtering, quiet hours, timezone |

## Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Runtime | Python 3.12 | async ecosystem |
| Framework | FastAPI + Uvicorn | lifespan for Telethon/aiogram, health endpoint |
| Userbot | Telethon 1.42 | MTProto, passive event handlers |
| Bot | aiogram 3.26 | FSM, middleware, inline keyboards, Bot API 9.4+ |
| AI | OpenRouter (free tier) | Qwen 3.6, Llama 3.3, Gemini Flash — configurable |
| DB | PostgreSQL | concurrent async writes, Railway-ready |
| ORM | SQLAlchemy 2.0 async | battle-tested, Alembic migrations |
| Scheduler | APScheduler 3.x | cron + interval triggers, PostgreSQL job store |
| Package manager | uv | 10-100x faster than pip |

## Setup

### 1. Clone and install

```bash
git clone https://github.com/iSevenpwnz/sift.git
cd sift
uv sync
```

### 2. Create `.env`

```bash
cp .env.example .env
```

Fill in:
- `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` — from [my.telegram.org](https://my.telegram.org)
- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `TELEGRAM_OWNER_ID` — your Telegram user ID
- `OPENROUTER_API_KEY` — from [openrouter.ai/keys](https://openrouter.ai/keys) (free, no card)
- `AI_MODEL` — e.g. `qwen/qwen3.6-plus-preview:free` (see `.env.example` for options)

### 3. Generate Telethon session

```bash
uv run python scripts/generate_session.py
```

Enter your phone number, SMS code. Copy the session string to `.env` → `TELETHON_SESSION`.

### 4. Start

```bash
# Start PostgreSQL
docker compose up postgres -d

# Run migrations
uv run alembic upgrade head

# Start app
uv run uvicorn src.app.main:app --host 0.0.0.0 --port 8100 --workers 1
```

### 5. Open your bot in Telegram and type `/start`

## Docker

```bash
docker compose up -d
```

App on `:8100`, PostgreSQL on `:5433`.

## AI Provider

Configurable via environment variables. All free, no credit card needed:

| Provider | Model | Speed |
|----------|-------|-------|
| OpenRouter | `qwen/qwen3.6-plus-preview:free` | ~10-25s |
| OpenRouter | `google/gemini-2.5-flash-lite:free` | ~1-3s |
| OpenRouter | `meta-llama/llama-3.3-70b-instruct:free` | ~5-15s |
| Groq | `llama-3.3-70b-versatile` | ~1-3s |

Fallback chain: primary provider → fallback provider → save to DB for retry.

## Cost

| Component | Cost |
|-----------|------|
| AI (OpenRouter free tier) | $0 |
| Telegram API | $0 |
| PostgreSQL | $0 (local) |
| **Total** | **$0** (local) or **~$5/mo** (Railway) |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design (written at project start, code has evolved significantly since).

### Key files

```
src/app/
  main.py              — FastAPI app + lifespan (entry point)
  config.py            — pydantic-settings config
  shared.py            — shared Telethon client
  constants.py         — shared constants
  collectors/
    telegram.py        — Telethon passive listener + chat approval
  processors/
    pipeline.py        — message processing: L1→L2→notify→summarize
    ai_provider.py     — OpenRouter/Gemini/Groq abstraction
    filter_l1.py       — regex pre-filter
  bot/
    dispatcher.py      — aiogram setup
    keyboards.py       — reply + inline keyboards
    handlers/
      commands.py      — /summary, /tasks, /catchup, /search, etc.
      callbacks.py     — inline button handlers + quick reply FSM
      settings.py      — /settings interactive menu
  scheduler/
    jobs.py            — daily digest, reminders, cleanup
  db/
    models.py          — Message, Task, Reminder, ChatDailySummary, UserSettings
    session.py         — async SQLAlchemy engine
prompts/
  categorize.txt       — AI categorization prompt (reasoning, reminders)
  update_summary.txt   — incremental summary prompt
  digest_summary.txt   — digest AI summary prompt
```

## License

MIT
