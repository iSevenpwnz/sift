from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeDefault, Message
from sqlalchemy import func, select

from src.app.bot.keyboards import main_keyboard, task_keyboard
from src.app.db.models import Message as DbMessage, Task
from src.app.db.session import async_session

router = Router()

CATEGORY_ICONS = {
    "meeting": "📅",
    "task": "📋",
    "deadline": "🔴",
    "info": "💡",
}


# ── Bot commands registration ───────────────────────────────

async def setup_bot_commands(bot) -> None:
    """Register bot commands in Telegram menu."""
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Перезапустити бота"),
            BotCommand(command="summary", description="Дайджест повідомлень"),
            BotCommand(command="tasks", description="Активні таски"),
            BotCommand(command="week", description="План на тиждень"),
            BotCommand(command="all", description="Всі повідомлення"),
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
    async with async_session() as session:
        today = func.current_date()

        result = await session.execute(
            select(DbMessage)
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.category.is_not(None))
            .where(DbMessage.category != "noise")
            .order_by(DbMessage.created_at.desc())
            .limit(15)
        )
        important = list(result.scalars().all())

        result = await session.execute(
            select(func.count()).select_from(DbMessage)
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.status.in_(["processed", "notified"]))
        )
        total = result.scalar_one()

        result = await session.execute(
            select(func.count()).select_from(DbMessage)
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.category == "noise")
        )
        noise_count = result.scalar_one()

    if not total:
        await message.answer("Сьогодні ще нічого не оброблено.", reply_markup=main_keyboard())
        return

    lines = [f"📊 <b>Дайджест</b>  •  {total} оброблено, {noise_count} шум\n"]

    if not important:
        lines.append("Нічого важливого не знайдено.")
    else:
        for msg in important:
            icon = CATEGORY_ICONS.get(msg.category, "💬")
            topic = msg.extracted_topic or msg.content[:80]
            chat = msg.source_chat or "—"
            sender = msg.sender or ""
            priority_mark = " ❗️" if msg.priority == "high" else ""
            time_str = msg.created_at.strftime("%H:%M") if msg.created_at else ""

            lines.append(f"{icon}{priority_mark} <b>{topic}</b>")
            lines.append(f"      <i>{chat} • {sender} • {time_str}</i>")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


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
