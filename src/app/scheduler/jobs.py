import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import func, select, distinct

from src.app.db.models import Message, Task
from src.app.db.session import async_session
from src.app.config import settings
from src.app.processors.pipeline import process_messages

logger = logging.getLogger(__name__)

CATEGORY_ICONS = {
    "meeting": "📅",
    "task": "📋",
    "deadline": "🔴",
    "info": "💡",
}

USER_TZ = ZoneInfo("Europe/Kyiv")


async def daily_digest(bot: Bot) -> None:
    """Send daily digest to owner."""
    async with async_session() as session:
        today = func.current_date()

        # Stats
        result = await session.execute(
            select(Message.category, func.count())
            .where(func.date(Message.created_at) == today)
            .where(Message.category.is_not(None))
            .group_by(Message.category)
        )
        stats = dict(result.all())

        # Important messages — deduplicate by topic
        result = await session.execute(
            select(Message)
            .where(func.date(Message.created_at) == today)
            .where(Message.category.in_(["meeting", "task", "deadline"]))
            .where(Message.source_chat != "Sift Hub")
            .where(Message.source_chat != "Unknown")
            .order_by(Message.created_at.desc())
            .limit(20)
        )
        all_important = list(result.scalars().all())

        # Deduplicate by topic (fuzzy — first 30 chars)
        seen_topics = set()
        important = []
        for msg in all_important:
            topic = (msg.extracted_topic or msg.content[:60]).strip().lower()
            key = topic[:30]  # fuzzy dedup by prefix
            if key not in seen_topics:
                seen_topics.add(key)
                important.append(msg)
            if len(important) >= 8:
                break

        # Missed during quiet hours
        result = await session.execute(
            select(Message)
            .where(func.date(Message.created_at) == today)
            .where(Message.priority == "high")
            .where(Message.status == "processed")
            .where(Message.source_chat != "Sift Hub")
            .where(Message.source_chat != "Unknown")
            .order_by(Message.created_at.desc())
            .limit(5)
        )
        missed = list(result.scalars().all())

        # Active tasks (not done, not snoozed)
        result = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .where((Task.snoozed_until.is_(None)) | (Task.snoozed_until <= func.now()))
            .order_by(Task.due_date.asc().nulls_last())
            .limit(10)
        )
        tasks = list(result.scalars().all())

    total = sum(stats.values())
    if not total and not tasks:
        return

    now = datetime.now(USER_TZ)
    lines = [f"☀️ <b>Доброго ранку! Дайджест за {now.strftime('%d.%m')}</b>\n"]

    noise = stats.get("noise", 0)
    useful = total - noise
    lines.append(f"💬 {total} повідомлень  •  {useful} корисних  •  {noise} шум\n")

    if missed:
        lines.append("🔕 <b>Пропущено (тихі години):</b>")
        for msg in missed:
            icon = CATEGORY_ICONS.get(msg.category, "💬")
            topic = msg.extracted_topic or msg.content[:60]
            lines.append(f"  {icon} {topic}")
        lines.append("")

    if important:
        lines.append("📌 <b>Важливе:</b>")
        for msg in important:
            icon = CATEGORY_ICONS.get(msg.category, "💬")
            topic = msg.extracted_topic or msg.content[:60]
            chat = msg.source_chat or ""
            time_str = msg.created_at.strftime("%H:%M") if msg.created_at else ""
            lines.append(f"  {icon} {topic}")
            lines.append(f"      <i>{chat} • {time_str}</i>")
        lines.append("")

    if tasks:
        lines.append(f"📋 <b>Активні таски ({len(tasks)}):</b>")
        for task in tasks:
            due = f" • до {task.due_date.strftime('%d.%m %H:%M')}" if task.due_date else ""
            lines.append(f"  • {task.title}{due}")
        lines.append("")
    else:
        lines.append("📋 Активних тасків: 0\n")

    lines.append("Гарного дня!")

    await bot.send_message(
        chat_id=settings.telegram_owner_id,
        text="\n".join(lines),
        parse_mode="HTML",
    )


async def retry_pending_ai(bot: Bot) -> None:
    """Retry messages stuck in pending_ai status."""
    async with async_session() as session:
        result = await session.execute(
            select(Message.id).where(Message.status == "pending_ai").order_by(Message.created_at).limit(20)
        )
        ids = list(result.scalars().all())

    if ids:
        logger.info(f"Retrying {len(ids)} pending AI messages")
        async with async_session() as session:
            result = await session.execute(select(Message).where(Message.id.in_(ids)))
            messages = list(result.scalars().all())
        if messages:
            await process_messages(messages, bot)
