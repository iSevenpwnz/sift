# Sift: Personal AI Hub

> Персональний AI-секретар в Telegram. Збирає повідомлення з усіх джерел, категоризує, нагадує про важливе.

## Концепт

Телефон (Telethon User API) пасивно слухає всі чати. AI фільтрує шум і витягує дії, зустрічі, дедлайни. Telegram Bot — єдиний інтерфейс для взаємодії.

```
┌─────────── Джерела (collectors) ──────────────────────┐
│                                                        │
│  Telegram (Telethon)     — групи, DM, канали   [MVP]  │
│  Slack (slack_sdk)       — Socket Mode          [v2]   │
│  Notetaker (webhook)     — Fireflies/Otter      [v2]   │
│  Gmail (Google API)      — push notifications   [v3]   │
│  Calendar (Google API)   — events/reminders     [v3]   │
│  GitHub (webhook)        — notifications        [v3]   │
│  RSS (feedparser)        — канали/блоги         [v2]   │
│                                                        │
└──────────────┬─────────────────────────────────────────┘
               │ unified InboxMessage
               ▼
┌─────────── Processing ────────────────────────────────┐
│                                                        │
│  L1: regex/keyword filter (безкоштовно)                │
│      - є дата/час? @mention мене? ключові слова?      │
│      - мем/стікер/лол? → skip                         │
│                                                        │
│  L2: AI categorization (Gemini Flash-Lite, free)      │
│      - категорія: meeting/task/deadline/info/noise    │
│      - extraction: дата, люди, тема, пріоритет        │
│      - JSON structured output                         │
│                                                        │
│  Storage: PostgreSQL (Railway, ~$0.50/мо)             │
│                                                        │
└──────────────┬─────────────────────────────────────────┘
               │
               ▼
┌─────────── Output (interface) ────────────────────────┐
│                                                        │
│  Telegram Bot (aiogram 3) — основний UI               │
│    - proactive notifications (high priority)          │
│    - /summary — дайджест дня                          │
│    - /tasks — активні таски [Done] [Snooze]           │
│    - /week — план на тиждень                          │
│    - /settings — налаштування фільтрів                │
│                                                        │
│  Notion API (опціонально) — structured storage  [v2]  │
│  Claude Code (локально) — deep analysis         [v2]  │
│                                                        │
└────────────────────────────────────────────────────────┘
```

---

## Стек технологій

| Компонент | Вибір | Чому |
|-----------|-------|------|
| **Runtime** | Python 3.12 | async ecosystem, Telethon/aiogram native |
| **Framework** | FastAPI + Uvicorn (1 worker) | HTTP webhooks + lifespan для Telethon/aiogram |
| **Userbot** | Telethon 1.42 (stable) | MTProto, passive event handlers, auto-reconnect. НЕ v2 (alpha) |
| **Bot** | aiogram 3.x | FSM, middleware, inline keyboards, scheduler integration |
| **AI** | Configurable provider (see AI Provider Strategy) | Gemini, OpenRouter, Groq — все free, вибір через env |
| **DB** | PostgreSQL (Railway) | Concurrent async writes, ~$0.50/мо, backups |
| **ORM** | SQLAlchemy 2.0 async + asyncpg | Battle-tested, proper async, Alembic migrations |
| **Scheduler** | APScheduler 3.11 AsyncIOScheduler | Cron triggers, PostgreSQL job store, NOT v4 (alpha) |
| **Retry** | tenacity | Async-native, exponential backoff + jitter |
| **Logging** | structlog | Structured JSON, ContextVars для async, Railway-friendly |
| **Package mgr** | uv + pyproject.toml | 10-100x faster than pip, lockfile, PEP 621 |
| **Deploy** | Railway Hobby ($5/мо) | Includes $5 credits, covers service + Postgres |

### Загальна вартість

| Компонент | Ціна/мо |
|-----------|---------|
| Railway Hobby (service + Postgres) | ~$5-7 |
| AI (Gemini / OpenRouter / Groq) | $0 (всі мають free tier) |
| Telegram API | $0 |
| Notion API | $0 |
| **Разом** | **~$5-7/мо** |

---

## Архітектура процесів

Один процес, один asyncio event loop, три підсистеми:

```
Uvicorn (manages event loop)
  │
  ├── FastAPI lifespan
  │     ├── start: connect Telethon, start aiogram, start APScheduler
  │     └── stop: disconnect all, flush DB, clean shutdown
  │
  ├── Telethon UserClient (asyncio.create_task)
  │     └── @client.on(events.NewMessage) → passive listener
  │         └── puts InboxMessage → asyncio.Queue
  │
  ├── aiogram Dispatcher (asyncio.create_task)
  │     └── dp.start_polling(bot, handle_signals=False)
  │         └── handles /summary, /tasks, /week, /settings, callbacks
  │
  ├── FastAPI routes (HTTP)
  │     ├── GET /health — Railway healthcheck
  │     ├── POST /webhook/slack — Slack events [v2]
  │     └── POST /webhook/notetaker — meeting summaries [v2]
  │
  ├── APScheduler (attached to loop)
  │     ├── check_rss — every 10 min [v2]
  │     ├── daily_digest — cron 09:00 UTC
  │     └── retry_failed — every 5 min
  │
  └── Message Processor (asyncio.create_task)
        └── reads from Queue → L1 filter → L2 AI → DB → notify bot
```

### Чому один процес

- Telethon тримає TCP з'єднання → Railway не засинає
- `--workers 1` обов'язково — multiple workers = multiple Telethon sessions = Telegram kills sessions
- Для нашого навантаження (сотні повідомлень/день) один процес — за очі

### Комунікація між компонентами

```
Telethon ──→ asyncio.Queue ──→ Processor ──→ DB
                                   │
                                   ├──→ bot.send_message() (high priority)
                                   └──→ DB (low priority, для /summary)
```

`asyncio.Queue` — найпростіший варіант для одного процесу. Якщо колись розділимо на окремі процеси — перейдемо на Redis pub/sub.

### Захист від втрати повідомлень (Queue loss при restart)

**Проблема:** asyncio.Queue живе в пам'яті. Якщо сервіс впав — повідомлення в черзі зникають. Telethon event handler отримує тільки НОВІ повідомлення, старі не re-fetch'аться.

**Рішення: write-ahead в DB перед Queue.**

```
Telethon event → DB INSERT (status='raw') → Queue.put(message_id)
                     ↑ persist first           ↓
                     │                    Processor reads from Queue
                     │                    DB UPDATE status='processed'
                     │
                     └── On startup: SELECT * WHERE status='raw' → re-queue
```

При старті сервісу — вичитати з DB всі `status='raw'` повідомлення і закинути в Queue. Жодне повідомлення не втрачається навіть при crash.

---

## Структура проєкту

```
sift/
├── pyproject.toml              # PEP 621, uv manages deps
├── uv.lock                     # committed lockfile
├── Dockerfile
├── docker-compose.yml          # local dev: app + postgres
├── .env.example
├── alembic.ini
├── README.md
│
├── src/
│   └── app/
│       ├── __init__.py
│       ├── main.py             # FastAPI app + lifespan (entry point)
│       ├── config.py           # pydantic-settings: all env vars
│       │
│       ├── db/
│       │   ├── models.py       # SQLAlchemy 2.0 models
│       │   ├── session.py      # async engine + sessionmaker
│       │   └── migrations/     # alembic versions
│       │
│       ├── collectors/
│       │   ├── base.py         # BaseCollector protocol
│       │   ├── telegram.py     # Telethon client + event handlers
│       │   ├── rss.py          # feedparser + APScheduler       [v2]
│       │   ├── slack.py        # slack_sdk Socket Mode           [v2]
│       │   └── webhook.py      # generic webhook receiver        [v2]
│       │
│       ├── processors/
│       │   ├── filter_l1.py    # regex/keyword pre-filter
│       │   ├── ai_provider.py  # AIProvider protocol + Gemini/OpenRouter/Groq impl
│       │   ├── pipeline.py     # L1 → L2 → DB → notify
│       │   └── notion.py       # Notion API client              [v2]
│       │
│       ├── bot/
│       │   ├── dispatcher.py   # aiogram Dispatcher + Router setup
│       │   ├── handlers/
│       │   │   ├── commands.py # /start, /summary, /tasks, /week
│       │   │   ├── callbacks.py# inline button handlers
│       │   │   └── settings.py # /settings FSM
│       │   ├── keyboards.py    # inline keyboard builders
│       │   └── formatters.py   # message formatting helpers
│       │
│       ├── scheduler/
│       │   ├── setup.py        # APScheduler config + job store
│       │   └── jobs.py         # daily_digest, retry_failed, etc.
│       │
│       └── api/
│           ├── health.py       # GET /health
│           └── webhooks.py     # POST /webhook/* routes          [v2]
│
├── tests/
│   ├── conftest.py
│   ├── test_filter_l1.py
│   ├── test_filter_l2.py
│   ├── test_pipeline.py
│   └── test_bot_handlers.py
│
└── scripts/
    ├── generate_session.py     # one-time: create Telethon StringSession
    └── seed_settings.py        # seed default user settings
```

---

## Database Schema

```sql
-- Вхідні повідомлення (з усіх джерел)
CREATE TABLE messages (
    id              SERIAL PRIMARY KEY,
    source          VARCHAR(32) NOT NULL,       -- telegram, slack, rss, webhook
    source_id       VARCHAR(255) UNIQUE,        -- dedup key
    source_chat     VARCHAR(255),               -- chat/channel name
    sender          VARCHAR(255),
    content         TEXT NOT NULL,
    content_type    VARCHAR(32) DEFAULT 'text', -- text, photo, voice, video, document
    reply_to_text   TEXT,                       -- текст повідомлення на яке reply (контекст)
    raw_metadata    JSONB DEFAULT '{}',         -- source-specific data

    -- Processing results (filled by AI)
    category        VARCHAR(32),                -- meeting, task, deadline, info, noise
    priority        VARCHAR(16),                -- high, low
    extracted_date  TIMESTAMPTZ,                -- дата з повідомлення
    extracted_people TEXT[],                     -- згадані люди
    extracted_topic VARCHAR(512),               -- короткий опис
    ai_response     JSONB DEFAULT '{}',         -- повна відповідь від AI

    -- Status lifecycle: raw → processed → notified → archived
    status          VARCHAR(32) DEFAULT 'raw',  -- raw, pending_ai, processed, notified, archived
    notified_at     TIMESTAMPTZ,

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Індекси для основних запитів
CREATE INDEX idx_messages_status ON messages(status);
CREATE INDEX idx_messages_status_created ON messages(status, created_at DESC);
CREATE INDEX idx_messages_source_created ON messages(source, created_at DESC);
CREATE INDEX idx_messages_category ON messages(category) WHERE category IS NOT NULL;
CREATE INDEX idx_messages_created_at ON messages(created_at DESC);

-- Таски витягнуті з повідомлень
CREATE TABLE tasks (
    id              SERIAL PRIMARY KEY,
    message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    title           VARCHAR(512) NOT NULL,
    due_date        TIMESTAMPTZ,
    is_done         BOOLEAN DEFAULT FALSE,
    done_at         TIMESTAMPTZ,
    snoozed_until   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tasks_active ON tasks(is_done, due_date) WHERE is_done = FALSE;
CREATE INDEX idx_tasks_snoozed ON tasks(snoozed_until) WHERE snoozed_until IS NOT NULL AND is_done = FALSE;

-- Нагадування (reminders)
CREATE TABLE reminders (
    id              SERIAL PRIMARY KEY,
    message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    task_id         INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    remind_at       TIMESTAMPTZ NOT NULL,
    sent            BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_reminders_pending ON reminders(remind_at) WHERE sent = FALSE;

-- Налаштування користувача
CREATE TABLE user_settings (
    id              SERIAL PRIMARY KEY,
    telegram_user_id BIGINT UNIQUE NOT NULL,
    monitored_chats JSONB DEFAULT '[]',         -- які чати слухати (порожній = всі)
    ignored_chats   JSONB DEFAULT '[]',         -- які чати ігнорувати
    quiet_hours     JSONB DEFAULT '{}',         -- {"start": "22:00", "end": "08:00"}
    digest_time     TIME DEFAULT '09:00',       -- коли дайджест
    timezone        VARCHAR(64) DEFAULT 'Europe/Kyiv',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- APScheduler job store (persistent jobs)
-- Створюється автоматично APScheduler'ом через SQLAlchemyJobStore

-- Архівація: cron job щомісяця
-- DELETE FROM messages WHERE status = 'archived' AND created_at < NOW() - INTERVAL '90 days';
-- Або: move to messages_archive table для аналітики
```

---

## Telethon: Session та безпека

### StringSession (обов'язково для Railway)

Telethon за замовчуванням створює `.session` SQLite файл. На Railway filesystem ephemeral — файл зникає при перезапуску.

**Рішення:** `StringSession` — вся сесія як base64 рядок в env var.

```
# Генерація (один раз, локально):
python scripts/generate_session.py
# → вводиш телефон, SMS код, 2FA
# → отримуєш рядок → копіюєш в Railway env TELETHON_SESSION
```

**Безпека:**
- StringSession = повний доступ до акаунту. Зберігати як секрет.
- Railway env vars зашифровані at rest.
- Генерувати ТІЛЬКИ локально, ніколи на сервері.

### Ризики cloud deployment

| Ризик | Реальність | Мітигація |
|-------|-----------|-----------|
| Бан акаунту з datacenter IP | Низький для особистого aged акаунту + passive listening | Не спамити, не скрейпити, тільки слухати |
| Session invalidation при зміні IP | Не відбувається (session прив'язана до auth key, не IP) | Чистий disconnect при SIGTERM |
| Два процеси з одною сесією | Telegram вбиває обидва | `--workers 1`, SIGTERM handling |
| FloodWait | Тільки при aggressive API calls | Passive event handler = push, не poll |

---

## AI Provider Strategy

Єдиний інтерфейс, змінний провайдер. Вибір через env var `AI_PROVIDER`.

### Підтримані провайдери (всі безкоштовні)

| Provider | Моделі | RPD (free) | JSON mode | Env var |
|----------|--------|------------|-----------|---------|
| **Google AI Studio** | Gemini 2.5 Flash-Lite | 1000 | native | `GEMINI_API_KEY` |
| **OpenRouter** | Qwen3.6-plus, Llama 3.3 70B, Gemma 3 27B, GPT-OSS 120B та ще ~25 free моделей | 200 (без кредитів) / 1000 ($10+ на балансі) | через prompt | `OPENROUTER_API_KEY` |
| **Groq** | Llama 3.3 70B, Qwen3 32B, GPT-OSS 120B | 1000-14400 | native | `GROQ_API_KEY` |

### Конфігурація

```bash
# .env — вибери один як primary, решта як fallback
AI_PROVIDER=openrouter                          # primary: gemini | openrouter | groq
AI_MODEL=qwen/qwen3.6-plus-preview:free        # model ID для обраного провайдера

AI_FALLBACK_PROVIDER=groq                       # fallback якщо primary недоступний
AI_FALLBACK_MODEL=llama-3.3-70b-versatile       # fallback model

GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
GROQ_API_KEY=...
```

### Архітектура в коді

```python
# processors/ai_provider.py
class AIProvider(Protocol):
    async def categorize(self, messages: list[dict]) -> list[dict]: ...

class GeminiProvider(AIProvider): ...
class OpenRouterProvider(AIProvider): ...   # OpenAI-compatible API
class GroqProvider(AIProvider): ...          # OpenAI-compatible API

def get_provider(name: str, model: str) -> AIProvider:
    providers = {"gemini": GeminiProvider, "openrouter": OpenRouterProvider, "groq": GroqProvider}
    return providers[name](model=model)
```

OpenRouter і Groq обидва використовують OpenAI-compatible API — один базовий клас, тільки `base_url` різний:
- OpenRouter: `https://openrouter.ai/api/v1`
- Groq: `https://api.groq.com/openai/v1`

### Fallback chain

```
Primary (AI_PROVIDER) → 429/5xx/timeout
  ↓
Fallback (AI_FALLBACK_PROVIDER) → 429/5xx/timeout
  ↓
Save to DB with status='pending_ai', retry через 5 хв
```

### OpenRouter: цікаві free моделі для нашого use case

| Модель | Чому підходить |
|--------|---------------|
| `qwen/qwen3.6-plus-preview:free` | 1M context, добре розуміє structured output |
| `meta-llama/llama-3.3-70b-instruct:free` | 65K context, сильний в extraction |
| `google/gemma-3-27b-it:free` | 131K context, добре з multilingual (UK/EN) |
| `openai/gpt-oss-120b:free` | Найбільша free модель, native structured output |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262K context, MoE |
| `openrouter/free` | Auto-router — сам вибирає найкращу доступну free модель |

**Рекомендація:** `openrouter/free` як primary (auto-routing, завжди є доступна модель), Groq як fallback (найшвидший inference).

---

## Дворівневий фільтр

### L1: Regex/keyword (безкоштовно, миттєво)

```python
# filter_l1.py — приклади правил
PASS_PATTERNS = [
    r'\d{1,2}[:.]\d{2}',           # час: 14:00, 9.30
    r'(зустріч|call|meeting|sync)',  # зустрічі
    r'(дедлайн|deadline|до п.ятниці|до кінця)',  # дедлайни
    r'(задача|task|TODO|треба|потрібно)',  # таски
    r'@\w+',                         # mentions
]

SKIP_PATTERNS = [
    r'^(лол|хаха|gg|nice|👍|😂)',   # реакції
    r'^https?://\S+$',              # тільки посилання без тексту
    # повідомлення < 10 символів без цифр
]

# Типи контенту
SKIP_CONTENT_TYPES = ['sticker', 'animation', 'video_note']  # стікери, гіфки, кружечки
PASS_WITH_CAPTION = ['photo', 'document']  # фото/документ з підписом → обробити caption
# voice → TODO v2: speech-to-text через Whisper/Gemini
```

**Мета:** відсіяти 80-90% шуму до AI. З 500 повідомлень/день → ~50-100 потрапляють на L2.

### L2: Gemini Flash-Lite (free, structured output)

**System prompt (в prompts/categorize.txt, не inline):**

```
You are a personal message assistant for a software engineer.
Your job: categorize incoming messages and extract actionable information.

Context about the user:
- Works as a developer at BRDG (fintech)
- Team members: Igor, Sanerok, Lytvynov
- Important topics: sprint reviews, PR reviews, deployments, bugs
- Languages: Ukrainian, English (messages can be mixed)

Categorize the message and extract structured data.
If the message is a reply, use the replied-to text for context.

Return JSON only, no explanation.
```

**Input format (batched — до 5 повідомлень за один запит):**

```json
{
  "messages": [
    {
      "id": 123,
      "chat": "BRDGE Dev",
      "sender": "Igor",
      "text": "зустріч завтра о 14 по DCF",
      "reply_to": "коли обговорюємо валюації?",
      "type": "text"
    }
  ]
}
```

**Response schema:**

```json
{
  "results": [
    {
      "id": 123,
      "category": "meeting",
      "priority": "high",
      "date": "2026-04-02T14:00:00",
      "people": ["Igor"],
      "topic": "DCF valuation discussion",
      "action_required": true
    }
  ]
}
```

**Batching:** повідомлення збираються в батчі по 5 (або по таймауту 30 секунд). Один AI запит замість п'яти. Економія rate limits.

**Fallback chain:** Gemini Flash-Lite → Groq (Llama 3.1 8B, 14400 RPD) → skip AI, save as status='pending_ai'.
**Retry:** tenacity з exponential backoff (1s → 2s → 4s → ... → 60s max, 5 attempts).
**Queue:** якщо AI лежить > 5 хвилин → зберегти в DB зі status='pending_ai', APScheduler retry кожні 5 хв.

---

## Bot інтерфейс (aiogram 3)

### Команди

| Команда | Що робить |
|---------|-----------|
| `/start` | Онбординг, створити user_settings |
| `/summary` | Дайджест: скільки повідомлень, категорії, top items |
| `/tasks` | Список активних тасків з кнопками [Done] [Snooze 1h] [Snooze 1d] |
| `/week` | План на тиждень: зустрічі, дедлайни |
| `/settings` | FSM: вибрати чати, quiet hours, timezone, digest time |
| `/mute 2h` | Тимчасово вимкнути нотифікації |

### Proactive notifications

```
[HIGH PRIORITY — негайно, зі звуком]
📋 Новий таск від @Sanerok:
"Переглянь PR #748 до завтра"
Джерело: BRDGE Dev Chat

[Done] [Snooze 1h] [Snooze tomorrow]
```

```
[LOW PRIORITY — тихо, disable_notification=True]
ℹ️ 3 нові повідомлення оброблено
Деталі: /summary
```

### Daily digest (APScheduler, 09:00 за timezone)

```
📊 Дайджест за 31 березня:

🔴 2 дедлайни:
  • PR #748 review — до 2 квітня
  • Sprint demo slides — до п'ятниці

📋 3 нових таски:
  • Переглянь PR #748
  • Оновити INFRASTRUCTURE.md
  • Відповісти Ігорю по DCF

📅 1 зустріч завтра:
  • Sprint review — 14:00

💬 47 повідомлень оброблено, 4 важливих
```

---

## Graceful Shutdown (Railway SIGTERM)

```python
# main.py — lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start
    await telethon_client.connect()
    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )
    processor_task = asyncio.create_task(message_processor())
    scheduler.start()

    yield

    # Shutdown (Railway sends SIGTERM, Uvicorn triggers this)
    scheduler.shutdown(wait=True)          # finish current jobs
    processor_task.cancel()                 # stop processor
    polling_task.cancel()                   # stop aiogram
    await telethon_client.disconnect()      # clean MTProto disconnect
    await db_engine.dispose()               # close DB pool
```

Railway дає 10 секунд між SIGTERM і SIGKILL. Цього достатньо для чистого shutdown.

---

## Docker

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy source
COPY src/ src/
COPY alembic.ini ./

# Shell form — потрібна для ${PORT} expansion (Docker exec form НЕ робить shell expansion)
CMD uv run uvicorn src.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
```

```yaml
# docker-compose.yml (local dev)
services:
  app:
    build: .
    env_file: .env
    ports:
      - "8000:8000"
    depends_on:
      - postgres
    volumes:
      - ./src:/app/src  # hot reload

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: sift
      POSTGRES_USER: sift
      POSTGRES_PASSWORD: localdev
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

---

## Environment Variables

```bash
# .env.example

# Telegram
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890
TELETHON_SESSION=...base64...        # StringSession (generate locally)
TELEGRAM_BOT_TOKEN=123456:ABC-DEF    # Bot from @BotFather
TELEGRAM_OWNER_ID=368222740          # твій Telegram user ID

# AI — configurable provider
AI_PROVIDER=openrouter               # gemini | openrouter | groq
AI_MODEL=openrouter/free             # model ID для провайдера
AI_FALLBACK_PROVIDER=groq            # fallback provider
AI_FALLBACK_MODEL=llama-3.3-70b-versatile

GEMINI_API_KEY=...                   # Google AI Studio (free)
OPENROUTER_API_KEY=...               # OpenRouter (free, no card)
GROQ_API_KEY=...                     # Groq (free, no card)

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/sift

# Optional
NOTION_TOKEN=...                     # Notion integration [v2]
NOTION_DATABASE_ID=...               # [v2]
LOG_LEVEL=INFO
DIGEST_HOUR=9                        # UTC hour for daily digest
TIMEZONE=Europe/Kyiv
```

---

## Roadmap

### MVP (v1) — 1-2 тижні
- [ ] Project skeleton: FastAPI + Telethon + aiogram + PostgreSQL
- [ ] Telethon passive listener для вибраних чатів
- [ ] L1 regex filter
- [ ] L2 Gemini Flash-Lite categorization
- [ ] Bot: /start, /summary, /tasks
- [ ] Proactive notifications (high priority)
- [ ] Daily digest
- [ ] Docker + Railway deploy
- [ ] generate_session.py script

### v2 — +1-2 тижні
- [ ] Bot: /week, /settings FSM, /mute
- [ ] Inline keyboards: [Done] [Snooze] на тасках
- [ ] RSS collector
- [ ] Notion integration
- [ ] Claude Code sync (GET /api/pending для check_inbox.py)
- [ ] Quiet hours

### v3 — коли буде час
- [ ] Slack collector (Socket Mode)
- [ ] Notetaker webhook (Fireflies/Otter)
- [ ] Gmail push notifications
- [ ] Google Calendar integration
- [ ] Telegram Mini App (dashboard)
- [ ] Multi-user support

---

## Крихкі моменти та мітигації

| Проблема | Що може піти не так | Рішення |
|----------|---------------------|---------|
| **Telethon session loss** | Railway redeploy = втрата файлової сесії | StringSession в env var, не файл |
| **Telegram бан** | Cloud IP + aggressive scraping | Passive listener only, aged personal account |
| **Gemini API down** | Повідомлення не категоризуються | Fallback на Groq; pending_ai queue в DB |
| **Free tier limits** | >1000 RPD на Gemini | L1 filter відсіює 80-90%; batching по 5; Groq fallback 14400 RPD |
| **Railway restart** | Втрата in-memory queue | Write-ahead в DB (status='raw') перед Queue; re-queue on startup |
| **Duplicate processing** | Два processes читають одне повідомлення | `--workers 1`; `source_id UNIQUE` constraint |
| **aiogram signal conflict** | SIGTERM handling conflict з Telethon | `handle_signals=False` при start_polling |
| **DB connection pool exhaustion** | Багато concurrent запитів | asyncpg pool з max_size=10 (більш ніж достатньо) |
| **Повідомлення пропущене** | Event handler crash | Wrap в try/except, log error, continue; write-ahead persist |
| **Timezone issues** | Дайджест о 9 ранку за UTC замість Kyiv | pydantic-settings timezone config, APScheduler timezone-aware triggers |
| **Edited messages** | Повідомлення змінилось після обробки | Telethon `events.MessageEdited` → re-process якщо вже в DB |
| **Telethon silent disconnect** | Не дізнаємось що відвалився | health_check job кожні 2 хв, бот нотифікує owner |
| **Webhook abuse** | Фейкові дані на POST /webhook/* | Signature verification (Slack), Bearer token (інші) |
| **Bot used by stranger** | Хтось знайшов бота і шле команди | OwnerOnlyMiddleware — silent ignore non-owner |
| **DB grows forever** | Сотні тисяч рядків за рік | Monthly archive: видалити archived > 90 днів |
| **Entity rate limits** | get_chat()/get_reply_message() = API calls | entity_cache dict; reply fetch тільки після L1 pass |

---

## Ключові рішення та обгрунтування

### Чому Telethon + aiogram, а не тільки Telethon?

Telethon може працювати як бот, але aiogram дає FSM, middleware, inline keyboard builders, router system. Для бота з /settings wizard, inline кнопками на тасках, і складною навігацією — aiogram значно продуктивніший.

### Чому PostgreSQL, а не SQLite?

- Railway filesystem ephemeral — SQLite потребує volume ($0.25/GB/мо + operational complexity)
- PostgreSQL на Railway ~$0.50/мо (дешевше volume!)
- Concurrent async writes без проблем (SQLite serializes writes через один thread)
- Alembic migrations працюють однаково

### Чому configurable AI provider, а не hardcoded?

- Free tier ліміти змінюються — сьогодні Gemini 1000 RPD, завтра може 100
- OpenRouter має ~28 free моделей, нові з'являються щомісяця
- Groq — найшвидший inference, ідеальний fallback
- Всі три провайдери безкоштовні і без кредитної картки
- OpenRouter і Groq використовують OpenAI-compatible API — мінімум коду
- Зміна провайдера = зміна env var, без redeploy коду

### Чому FastAPI якщо бот працює без HTTP?

- Railway потребує healthcheck endpoint
- Webhooks для Slack, notetaker, etc. (v2)
- `lifespan` — ідеальний pattern для startup/shutdown всіх компонентів
- Zero overhead — Uvicorn вже крутить event loop

### Чому NOT Pyrogram?

- Оригінальний pyrogram/pyrogram **заархівований** (Dec 2024)
- Активні форки (Pyrofork) — менша спільнота, менше підтримки
- Для нового проєкту немає причини залежати від форку

---

## Інтеграція з Claude Code (v2)

Поточний сетап (локальний collector + /telegram-inbox skill) залишається.
Додається sync з Railway:

```
# check_inbox.py (оновлений)
# 1. Try Railway API
# 2. Fallback to local inbox.json
# 3. Show combined results

# Railway endpoint:
# GET /api/pending?status=pending_review → повідомлення що потребують review
# PATCH /api/messages/{id}/done → позначити оброблене
```

Claude Code = "важка артилерія" для складного аналізу.
Railway bot = автоматика для рутини.

---

## Безпека

### Bot access control

Бот працює тільки для owner. Кожен handler перевіряє `telegram_user_id`:

```python
# bot/middleware.py — aiogram middleware
class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if event.from_user.id != settings.TELEGRAM_OWNER_ID:
            return  # ignore silently
        return await handler(event, data)
```

Реєструється глобально на dispatcher — жоден handler не працює без перевірки.

### Webhook authentication

POST /webhook/* endpoints захищені:

```python
# api/webhooks.py
@router.post("/webhook/slack")
async def slack_webhook(request: Request):
    # Verify Slack signature (X-Slack-Signature + signing secret)
    if not verify_slack_signature(request):
        raise HTTPException(403)
    ...

@router.post("/webhook/notetaker")
async def notetaker_webhook(
    request: Request,
    token: str = Header(alias="X-Webhook-Token")
):
    if token != settings.WEBHOOK_SECRET:
        raise HTTPException(403)
    ...
```

### API auth для Claude Code sync

```
GET /api/pending — requires Authorization: Bearer <API_KEY>
PATCH /api/messages/{id}/done — same
```

`API_KEY` — окремий env var, не пов'язаний з іншими токенами.

---

## Моніторинг та health

### /health endpoint

```json
GET /health
{
  "status": "ok",
  "telethon_connected": true,
  "last_message_at": "2026-04-01T15:30:00Z",    // null якщо ніколи
  "pending_messages": 3,                          // status='raw' + 'pending_ai'
  "uptime_seconds": 86400
}
```

### Self-monitoring через бота

Якщо Telethon відключився і не перепідключився за 5 хвилин — бот надсилає повідомлення owner:

```
⚠️ Telethon disconnected at 15:30.
Auto-reconnect failed after 3 attempts.
Service will restart in 60s.
```

APScheduler job `health_check` кожні 2 хвилини перевіряє:
- Telethon connected?
- Остання message менше 1 години тому? (якщо ні і це робочий час — можливо проблема)
- Pending AI queue не росте безконтрольно?

---

## Обробка медіа та контексту

### Типи повідомлень

| Тип | Обробка |
|-----|---------|
| **text** | Повний pipeline (L1 → L2) |
| **photo з caption** | Обробляємо caption як текст |
| **document з caption** | Обробляємо caption як текст |
| **voice** | MVP: skip. v2: Gemini multimodal або Whisper STT |
| **video_note (кружечок)** | Skip |
| **sticker** | Skip |
| **animation (gif)** | Skip |
| **edited message** | Перезаписати content в DB, re-process через AI якщо категорія змінилась |

### Reply context

Telethon дає `event.reply_to_msg_id`. Щоб отримати текст replied-to повідомлення:

```python
@client.on(events.NewMessage)
async def handler(event):
    reply_text = None
    if event.reply_to_msg_id:
        reply_msg = await event.get_reply_message()
        reply_text = reply_msg.text if reply_msg else None

    # Save both: content + reply_to_text
```

**Важливо:** `get_reply_message()` це API call. Кешувати entity resolution. Не викликати для кожного повідомлення в активному чаті — тільки якщо L1 filter пропустив.

### Entity caching

Telethon resolves username → entity через API call (rate limited). Кешуємо:

```python
# collectors/telegram.py
entity_cache: dict[int, str] = {}  # chat_id → chat_title

@client.on(events.NewMessage)
async def handler(event):
    chat_title = entity_cache.get(event.chat_id)
    if not chat_title:
        chat = await event.get_chat()
        chat_title = getattr(chat, 'title', getattr(chat, 'first_name', 'Unknown'))
        entity_cache[event.chat_id] = chat_title
```

---

## Локальна розробка та тестування

### Без реального TG акаунту

```python
# tests/conftest.py
@pytest.fixture
def sample_messages():
    """Фікстури з реальних повідомлень (анонімізовані)."""
    return [
        InboxMessage(source="telegram", content="зустріч завтра о 14", ...),
        InboxMessage(source="telegram", content="лол 😂", ...),
        InboxMessage(source="telegram", content="PR #748 треба заревʼюїти до завтра", ...),
    ]
```

**Unit tests** — filter_l1, filter_l2 (mock AI response), pipeline logic.
**Integration tests** — з реальною PostgreSQL (docker-compose test profile).
**Telethon/aiogram** — НЕ тестуємо напряму. Тонкий adapter layer, вся логіка в processors/.

### Dev mode

```bash
# docker-compose.yml має dev profile
docker compose up postgres    # тільки DB
uv run python -m src.app.main  # app з hot reload

# Або через test bot (окремий @BotFather бот для dev):
TELEGRAM_BOT_TOKEN=dev_bot_token uv run ...
```

### Seed data

```bash
uv run python scripts/seed_settings.py   # створити user_settings для owner
uv run python scripts/seed_messages.py   # тестові повідомлення в DB
```

---

## Git та CI/CD

### .gitignore

```
.env
*.session
__pycache__/
.venv/
*.pyc
.mypy_cache/
.ruff_cache/
```

### Branch strategy

- `main` — production (Railway auto-deploys)
- `develop` — development
- feature branches → PR → develop → main

### CI (GitHub Actions, мінімальний)

```yaml
# .github/workflows/ci.yml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: test
          POSTGRES_PASSWORD: test
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --frozen
      - run: uv run ruff check src/
      - run: uv run pytest tests/ -v
```

### Railway auto-deploy

Railway підключається до GitHub repo. Push в `main` → автоматичний deploy. Нема потреби в окремому CD pipeline.
