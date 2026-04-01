import logging

from aiogram import Bot
from sqlalchemy import func, select

from src.app.db.models import Message, Task
from src.app.db.session import async_session
from src.app.config import settings
from src.app.processors.pipeline import claim_raw_messages, process_messages

logger = logging.getLogger(__name__)


async def daily_digest(bot: Bot) -> None:
    """Send daily digest to owner. Scheduled via APScheduler."""
    async with async_session() as session:
        today = func.current_date()

        # Message stats
        result = await session.execute(
            select(Message.category, func.count())
            .where(func.date(Message.created_at) == today)
            .where(Message.category.is_not(None))
            .group_by(Message.category)
        )
        stats = dict(result.all())

        # Important unnotified messages
        result = await session.execute(
            select(Message)
            .where(Message.priority == "high")
            .where(Message.status == "processed")
            .where(func.date(Message.created_at) == today)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        important = list(result.scalars().all())

        # Active tasks
        result = await session.execute(
            select(Task).where(Task.is_done.is_(False)).order_by(Task.due_date.asc().nulls_last()).limit(5)
        )
        tasks = list(result.scalars().all())

    total = sum(stats.values())
    if not total and not tasks:
        return  # Nothing to report

    lines = [f"Daily digest:\n"]

    if total:
        lines.append(f"{total} messages processed")
        for cat in ["meeting", "deadline", "task", "info"]:
            count = stats.get(cat, 0)
            if count:
                lines.append(f"  {cat}: {count}")
        lines.append("")

    if important:
        lines.append("Important:")
        for msg in important:
            topic = msg.extracted_topic or msg.content[:80]
            lines.append(f"  - {topic} ({msg.source_chat})")
        lines.append("")

    if tasks:
        lines.append("Active tasks:")
        for task in tasks:
            due = f" (due: {task.due_date.strftime('%d.%m')})" if task.due_date else ""
            lines.append(f"  - {task.title}{due}")

    await bot.send_message(chat_id=settings.telegram_owner_id, text="\n".join(lines))


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
            from src.app.db.models import Message as MsgModel
            result = await session.execute(select(MsgModel).where(MsgModel.id.in_(ids)))
            messages = list(result.scalars().all())
        if messages:
            await process_messages(messages, bot)
