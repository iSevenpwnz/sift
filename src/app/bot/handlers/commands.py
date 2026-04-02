from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeDefault, Message
from sqlalchemy import func, select

from src.app.bot.keyboards import main_keyboard, task_keyboard
from src.app.constants import CATEGORY_ICONS
from src.app.db.models import Message as DbMessage, Task
from src.app.db.session import async_session

router = Router()


# ── Bot commands registration ───────────────────────────────

async def setup_bot_commands(bot) -> None:
    """Register bot commands in Telegram menu."""
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Перезапустити бота"),
            BotCommand(command="summary", description="Дайджест повідомлень"),
            BotCommand(command="tasks", description="Активні таски"),
            BotCommand(command="week", description="План на тиждень"),
            BotCommand(command="search", description="Пошук повідомлень"),
            BotCommand(command="history", description="Історія за дату"),
            BotCommand(command="mute", description="Вимкнути нотифікації (1h)"),
            BotCommand(command="unmute", description="Увімкнути нотифікації"),
            BotCommand(command="status", description="Статус системи"),
            BotCommand(command="settings", description="Налаштування"),
        ],
        scope=BotCommandScopeDefault(),
    )


# ── /start ──────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Sift</b> — просіює шум, залишає важливе.\n\n"
        "Я моніторю твої чати і повідомлю коли щось важливе.\n"
        "Використовуй кнопки внизу для навігації.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ── Summary (slash + button) ────────────────────────────────

@router.message(Command("summary"))
@router.message(F.text == "📊 Дайджест")
async def cmd_summary(message: Message) -> None:
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    from src.app.scheduler.jobs import build_digest

    loading = await message.answer("⏳ Генерую дайджест...")
    today = dt.now(ZoneInfo("Europe/Kyiv")).date()
    content, keyboard = await build_digest(today)

    from aiogram.types import LinkPreviewOptions
    no_preview = LinkPreviewOptions(is_disabled=True)

    await loading.delete()
    if isinstance(content, list):
        for i, part in enumerate(content):
            is_last = i == len(content) - 1
            await message.answer(
                text=part, parse_mode="HTML", link_preview_options=no_preview,
                reply_markup=keyboard if is_last else None,
            )
    else:
        await message.answer(
            text=content, parse_mode="HTML", link_preview_options=no_preview,
            reply_markup=keyboard,
        )


# ── Tasks (slash + button) ──────────────────────────────────

@router.message(Command("tasks"))
@router.message(F.text == "📋 Таски")
async def cmd_tasks(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .where((Task.snoozed_until.is_(None)) | (Task.snoozed_until <= func.now()))
            .order_by(Task.due_date.asc().nulls_last())
            .limit(20)
        )
        tasks = list(result.scalars().all())

    if not tasks:
        await message.answer("📋 Активних тасків немає.", reply_markup=main_keyboard())
        return

    # First message with count
    await message.answer(
        f"📋 <b>Активні таски</b>  •  {len(tasks)} шт.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    # Each task as separate message with inline buttons
    for task in tasks:
        due = f"\n📆 До: {task.due_date.strftime('%d.%m %H:%M')}" if task.due_date else ""
        text = f"<b>{task.title}</b>{due}"
        await message.answer(text, parse_mode="HTML", reply_markup=task_keyboard(task.id))


# ── Week (slash + button) ───────────────────────────────────

@router.message(Command("week"))
@router.message(F.text == "📅 Тиждень")
async def cmd_week(message: Message) -> None:
    from datetime import datetime, timedelta, timezone

    async with async_session() as session:
        now = datetime.now(timezone.utc)
        week_end = now + timedelta(days=7)

        result = await session.execute(
            select(DbMessage)
            .where(DbMessage.extracted_date.between(now, week_end))
            .where(DbMessage.category.in_(["meeting", "deadline"]))
            .order_by(DbMessage.extracted_date.asc())
            .limit(20)
        )
        events = list(result.scalars().all())

        result = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .where(Task.due_date.between(now, week_end))
            .order_by(Task.due_date.asc())
            .limit(20)
        )
        tasks = list(result.scalars().all())

    if not events and not tasks:
        await message.answer("📅 На цьому тижні нічого заплановано.", reply_markup=main_keyboard())
        return

    lines = ["📅 <b>План на тиждень</b>\n"]

    if events:
        for ev in events:
            icon = CATEGORY_ICONS.get(ev.category, "📌")
            date_str = ev.extracted_date.strftime("%d.%m %H:%M") if ev.extracted_date else ""
            topic = ev.extracted_topic or ev.content[:80]
            lines.append(f"{icon} <b>{date_str}</b> — {topic}")
        lines.append("")

    if tasks:
        lines.append("<b>Таски з дедлайном:</b>")
        for task in tasks:
            due = task.due_date.strftime("%d.%m") if task.due_date else ""
            lines.append(f"📋 <b>{due}</b> — {task.title}")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


# ── All messages ────────────────────────────────────────────

@router.message(Command("all"))
async def cmd_all(message: Message) -> None:
    async with async_session() as session:
        today = func.current_date()
        result = await session.execute(
            select(DbMessage)
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.category.is_not(None))
            .where(DbMessage.category != "noise")
            .order_by(DbMessage.created_at.desc())
            .limit(30)
        )
        messages = list(result.scalars().all())

    if not messages:
        await message.answer("Сьогодні нічого важливого.", reply_markup=main_keyboard())
        return

    await message.answer(
        f"📝 <b>Всі повідомлення за сьогодні</b>  •  {len(messages)} шт.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    for msg in messages:
        icon = CATEGORY_ICONS.get(msg.category, "💬")
        topic = msg.extracted_topic or ""
        content_preview = msg.content[:200]
        chat = msg.source_chat or "—"
        sender = msg.sender or ""
        time_str = msg.created_at.strftime("%H:%M") if msg.created_at else ""

        text = f"{icon} <b>{topic}</b>\n"
        text += f"<i>{chat} • {sender} • {time_str}</i>\n\n"
        text += content_preview

        await message.answer(text, parse_mode="HTML")


# ── History ─────────────────────────────────────────────────

@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    """Show digest for a specific date. Usage: /history 31.03 or /history yesterday."""
    from datetime import datetime as dt, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Kyiv")
    args = message.text.split(maxsplit=1)
    target_date = None

    if len(args) > 1:
        arg = args[1].strip().lower()
        if arg in ("вчора", "yesterday"):
            target_date = (dt.now(tz) - timedelta(days=1)).date()
        else:
            for fmt in ("%d.%m", "%d.%m.%Y", "%Y-%m-%d"):
                try:
                    parsed = dt.strptime(arg, fmt)
                    if fmt == "%d.%m":
                        parsed = parsed.replace(year=dt.now().year)
                    target_date = parsed.date()
                    break
                except ValueError:
                    continue

    if not target_date:
        await message.answer(
            "📅 <b>Використання:</b>\n"
            "<code>/history вчора</code>\n"
            "<code>/history 31.03</code>\n"
            "<code>/history 25.03.2026</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    async with async_session() as session:
        result = await session.execute(
            select(DbMessage)
            .where(func.date(DbMessage.created_at) == target_date)
            .where(DbMessage.category.is_not(None))
            .where(DbMessage.category != "noise")
            .order_by(DbMessage.created_at.desc())
            .limit(20)
        )
        messages = list(result.scalars().all())

        result = await session.execute(
            select(func.count()).select_from(DbMessage)
            .where(func.date(DbMessage.created_at) == target_date)
        )
        total = result.scalar_one()

    date_str = target_date.strftime("%d.%m.%Y")

    if not messages:
        await message.answer(
            f"📅 <b>{date_str}</b> — нічого важливого ({total} повідомлень).",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    lines = [f"📅 <b>Історія за {date_str}</b>  •  {len(messages)} важливих з {total}\n"]

    for msg in messages:
        icon = CATEGORY_ICONS.get(msg.category, "💬")
        topic = msg.extracted_topic or msg.content[:80]
        chat = msg.source_chat or "—"
        time_str = msg.created_at.strftime("%H:%M") if msg.created_at else ""
        lines.append(f"{icon} <b>{topic}</b>")
        lines.append(f"      <i>{chat} • {time_str}</i>")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


# ── Mute ────────────────────────────────────────────────────

@router.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    """Mute notifications. Usage: /mute 2 (hours, default 1)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from src.app.db.models import UserSettings

    args = message.text.split()
    hours = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1

    tz = ZoneInfo("Europe/Kyiv")
    until = datetime.now(tz) + timedelta(hours=hours)

    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == message.from_user.id)
        )
        us = result.scalar_one_or_none()
        if us:
            qh = dict(us.quiet_hours or {})
            qh["muted_until"] = until.isoformat()
            us.quiet_hours = qh
            await session.commit()

    until_str = until.strftime("%H:%M")
    await message.answer(
        f"🔇 <b>Нотифікації вимкнено до {until_str}</b>\n"
        f"Повідомлення збиратимуться і будуть в /summary.\n"
        f"Щоб увімкнути: /unmute",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@router.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    from src.app.db.models import UserSettings

    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == message.from_user.id)
        )
        us = result.scalar_one_or_none()
        if us:
            qh = dict(us.quiet_hours or {})
            qh.pop("muted_until", None)
            us.quiet_hours = qh
            await session.commit()

    await message.answer("🔔 <b>Нотифікації увімкнено!</b>", parse_mode="HTML", reply_markup=main_keyboard())


# ── Status ──────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    from src.app.db.models import UserSettings

    async with async_session() as session:
        # Counts
        total = (await session.execute(select(func.count()).select_from(DbMessage))).scalar_one()
        today_count = (await session.execute(
            select(func.count()).select_from(DbMessage).where(func.date(DbMessage.created_at) == func.current_date())
        )).scalar_one()
        raw_count = (await session.execute(
            select(func.count()).select_from(DbMessage).where(DbMessage.status == "raw")
        )).scalar_one()
        tasks_count = (await session.execute(
            select(func.count()).select_from(Task).where(Task.is_done.is_(False))
        )).scalar_one()

        # Settings
        result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == message.from_user.id)
        )
        us = result.scalar_one_or_none()

    monitored = len(us.monitored_chats or []) if us else 0
    ignored = len(us.ignored_chats or []) if us else 0

    muted_until = (us.quiet_hours or {}).get("muted_until") if us else None
    mute_str = "🔇 вимкнено" if muted_until else "🔔 увімкнено"

    lines = [
        "📊 <b>Статус Sift</b>\n",
        f"💬 Повідомлень: {total} всього, {today_count} сьогодні",
        f"⏳ В черзі: {raw_count}",
        f"📋 Активних тасків: {tasks_count}",
        f"\n🔔 Моніторимо чатів: {monitored}",
        f"🔇 Ігноруємо чатів: {ignored}",
        f"📢 Нотифікації: {mute_str}",
    ]

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


# ── Search ──────────────────────────────────────────────────

@router.message(Command("search"))
async def cmd_search(message: Message) -> None:
    """Search messages. Usage: /search keyword"""
    import html as html_mod
    from aiogram.types import LinkPreviewOptions

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            "🔍 <b>Пошук</b>\n\n"
            "<code>/search ключове слово</code>\n"
            "<code>/search Ігор зустріч</code>\n"
            "<code>/search DCF</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    query = args[1].strip()
    e = html_mod.escape

    async with async_session() as session:
        pattern = f"%{query}%"
        result = await session.execute(
            select(DbMessage)
            .where(
                (DbMessage.content.ilike(pattern)) | (DbMessage.extracted_topic.ilike(pattern))
            )
            .where(DbMessage.category.is_not(None))
            .order_by(DbMessage.created_at.desc())
            .limit(15)
        )
        messages = list(result.scalars().all())

    if not messages:
        await message.answer(
            f"🔍 <b>{e(query)}</b> — нічого не знайдено.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    lines = [f"🔍 <b>{e(query)}</b> — {len(messages)} результатів\n"]

    for msg in messages:
        icon = CATEGORY_ICONS.get(msg.category, "💬")
        topic = msg.extracted_topic or msg.content[:60]
        chat = msg.source_chat or "—"
        date_str = msg.created_at.strftime("%d.%m %H:%M") if msg.created_at else ""

        meta = msg.raw_metadata or {}
        cid = str(meta.get("chat_id", ""))
        mid = meta.get("message_id")
        link = f"https://t.me/c/{cid[4:]}/{mid}" if cid.startswith("-100") and mid else None

        if link:
            lines.append(f'{icon} <a href="{link}">{e(topic)}</a>')
        else:
            lines.append(f"{icon} {e(topic)}")
        lines.append(f"    <i>{e(chat)} • {date_str}</i>\n")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n\n<i>...обрізано</i>"

    await message.answer(
        text,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=main_keyboard(),
    )
