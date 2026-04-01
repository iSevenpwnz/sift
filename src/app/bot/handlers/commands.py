from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from src.app.db.models import Message as DbMessage, Task
from src.app.db.session import async_session
from src.app.bot.keyboards import task_keyboard

router = Router()

CATEGORY_ICONS = {
    "meeting": "📅",
    "task": "📋",
    "deadline": "🔴",
    "info": "💡",
}


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "<b>Sift</b> — sifts signal from noise.\n\n"
        "/summary — дайджест за сьогодні\n"
        "/tasks — активні таски\n"
        "/week — план на тиждень\n"
        "/all — всі повідомлення за сьогодні",
        parse_mode="HTML",
    )


@router.message(Command("summary"))
async def cmd_summary(message: Message) -> None:
    async with async_session() as session:
        today = func.current_date()

        # Important messages (not noise)
        result = await session.execute(
            select(DbMessage)
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.category.is_not(None))
            .where(DbMessage.category != "noise")
            .order_by(DbMessage.created_at.desc())
            .limit(20)
        )
        important = list(result.scalars().all())

        # Total stats
        result = await session.execute(
            select(func.count())
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.status.in_(["processed", "notified"]))
        )
        total = result.scalar_one()

        result = await session.execute(
            select(func.count())
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.category == "noise")
        )
        noise_count = result.scalar_one()

    if not total:
        await message.answer("Сьогодні ще нічого не оброблено.")
        return

    lines = [f"<b>Дайджест за сьогодні</b> ({total} повідомлень, {noise_count} шум)\n"]

    if not important:
        lines.append("Нічого важливого не знайдено.")
    else:
        for msg in important:
            icon = CATEGORY_ICONS.get(msg.category, "💬")
            topic = msg.extracted_topic or msg.content[:80]
            chat = msg.source_chat or "—"
            sender = msg.sender or ""
            priority_mark = " ❗️" if msg.priority == "high" else ""

            lines.append(f"{icon}{priority_mark} <b>{topic}</b>")
            lines.append(f"    {chat} — {sender}")
            lines.append("")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("tasks"))
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
        await message.answer("Активних тасків немає.")
        return

    for task in tasks:
        due = f"\nDue: {task.due_date.strftime('%d.%m %H:%M')}" if task.due_date else ""
        text = f"📋 <b>{task.title}</b>{due}"
        await message.answer(text, parse_mode="HTML", reply_markup=task_keyboard(task.id))


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    async with async_session() as session:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        week_end = now + timedelta(days=7)

        # Meetings & deadlines this week
        result = await session.execute(
            select(DbMessage)
            .where(DbMessage.extracted_date.between(now, week_end))
            .where(DbMessage.category.in_(["meeting", "deadline"]))
            .order_by(DbMessage.extracted_date.asc())
            .limit(20)
        )
        events = list(result.scalars().all())

        # Tasks with due date this week
        result = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .where(Task.due_date.between(now, week_end))
            .order_by(Task.due_date.asc())
            .limit(20)
        )
        tasks = list(result.scalars().all())

    if not events and not tasks:
        await message.answer("На цьому тижні нічого заплановано.")
        return

    lines = ["<b>План на тиждень</b>\n"]

    if events:
        for ev in events:
            icon = CATEGORY_ICONS.get(ev.category, "📌")
            date_str = ev.extracted_date.strftime("%d.%m %H:%M") if ev.extracted_date else ""
            topic = ev.extracted_topic or ev.content[:80]
            lines.append(f"{icon} {date_str} — {topic}")
        lines.append("")

    if tasks:
        lines.append("<b>Таски:</b>")
        for task in tasks:
            due = task.due_date.strftime("%d.%m") if task.due_date else ""
            lines.append(f"📋 {due} — {task.title}")

    await message.answer("\n".join(lines), parse_mode="HTML")


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
            .limit(50)
        )
        messages = list(result.scalars().all())

    if not messages:
        await message.answer("Сьогодні нічого важливого.")
        return

    for msg in messages:
        icon = CATEGORY_ICONS.get(msg.category, "💬")
        topic = msg.extracted_topic or ""
        content_preview = msg.content[:200]
        chat = msg.source_chat or "—"
        sender = msg.sender or ""
        time_str = msg.created_at.strftime("%H:%M") if msg.created_at else ""

        text = f"{icon} <b>{topic}</b>\n"
        text += f"<i>{chat} — {sender} о {time_str}</i>\n"
        text += f"{content_preview}"

        await message.answer(text, parse_mode="HTML")
