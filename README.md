# Sift

**Personal AI hub — sifts signal from noise across your messaging channels.**

Sift passively monitors your Telegram chats, categorizes messages with AI, and sends you notifications about what actually matters. A Telegram bot serves as the single interface for everything.

## How it works

```
Telegram chats (Telethon) → AI categorization (OpenRouter/Gemini/Groq) → Bot notifications
```

1. **Telethon** reads your chats passively (groups, DMs, channels)
2. **Two-level filter**: regex pre-filter → AI categorization (meeting/task/deadline/info/noise)
3. **Bot notifies** you about important messages with inline action buttons
4. **Daily digest** groups everything by chat with AI summaries and clickable links

## Features

- **Proactive notifications** — bot messages you when something important happens
- **Smart filtering** — two-level filter (regex + AI) reduces noise by 80-90%
- **Chat approval** — new chats detected → bot asks [Monitor] [Ignore]
- **Grouped digest** — messages grouped by chat (DMs → Groups → Channels) with expandable quotes
- **Clickable links** — each news item links to the original message
- **Task management** — AI extracts tasks/meetings, [Done] [Snooze 1h] [Snooze 1d] buttons
- **Search** — `/search keyword` across all messages
- **History** — `/history 31.03` for any date
- **Quiet hours** — no notifications during configured hours
- **Mute** — `/mute 2` to silence for 2 hours
- **Settings** — interactive inline menu for chat filtering, timezone, digest time

## Bot commands

| Command | Description |
|---------|-------------|
| `/summary` | Daily digest (or tap "Дайджест" button) |
| `/tasks` | Active tasks with action buttons |
| `/week` | This week's meetings and deadlines |
| `/search <query>` | Search all messages |
| `/history <date>` | Digest for a specific date |
| `/mute [hours]` | Mute notifications |
| `/unmute` | Unmute |
| `/status` | System stats |
| `/settings` | Chat filtering, quiet hours, timezone |

## Stack

| Component | Choice |
|-----------|--------|
| Runtime | Python 3.12 |
| Framework | FastAPI + Uvicorn |
| Userbot | Telethon 1.42 |
| Bot | aiogram 3.x |
| AI | OpenRouter (Qwen, Llama, Gemma — free tier) |
| DB | PostgreSQL |
| ORM | SQLAlchemy 2.0 async |
| Scheduler | APScheduler 3.x |
| Package manager | uv |

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

### 5. Open @YourBot in Telegram and type `/start`

## Docker

```bash
docker compose up -d
```

App on `:8100`, PostgreSQL on `:5433`.

## Cost

| Component | Cost |
|-----------|------|
| AI (OpenRouter/Gemini/Groq free tier) | $0 |
| Telegram API | $0 |
| PostgreSQL | $0 (local) |
| **Total** | **$0** (local) or **~$5/mo** (Railway) |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design.

## License

MIT
