import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.app.db.models import Message, Task
from src.app.db.session import async_session
from src.app.processors.ai_provider import get_fallback_provider, get_primary_provider
from src.app.processors.filter_l1 import should_process
from src.app.bot.keyboards import task_keyboard
from src.app.config import settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
BATCH_TIMEOUT = 5  # seconds

CATEGORY_ICONS = {
    "meeting": "📅",
    "task": "📋",
    "deadline": "🔴",
    "info": "💡",
}


async def persist_raw(message_data: dict) -> int | None:
    """Write-ahead: save to DB before processing. Returns message ID or None if duplicate."""
    async with async_session() as session:
        stmt = (
            pg_insert(Message)
            .values(
                source=message_data["source"],
                source_id=message_data["source_id"],
                source_chat=message_data.get("source_chat"),
                sender=message_data.get("sender"),
                content=message_data["content"],
                content_type=message_data.get("content_type", "text"),
                reply_to_text=message_data.get("reply_to_text"),
                raw_metadata=message_data.get("raw_metadata", {}),
                status="raw",
            )
            .on_conflict_do_update(
                index_elements=["source_id"],
                set_={"content": message_data["content"], "updated_at": func.now()},
            )
            .returning(Message.id)
        )
        result = await session.execute(stmt)
        await session.commit()
        row = result.scalar_one_or_none()
        return row


async def notify_owner(bot: Bot, msg: Message, ai_result: dict, task: Task | None = None) -> None:
    """Send proactive notification to owner for important messages."""
    category = ai_result.get("category", "info")
    priority = ai_result.get("priority", "low")

    # Skip noise and low-priority info
    if category == "noise":
        return
    if category == "info" and priority == "low":
        return

    icon = CATEGORY_ICONS.get(category, "💬")
    topic = ai_result.get("topic") or msg.content[:100]
    chat = msg.source_chat or "—"
    sender = msg.sender or ""

    text = f"{icon} <b>{topic}</b>\n"
    text += f"<i>{chat} — {sender}</i>\n"

    if ai_result.get("date"):
        text += f"📆 {ai_result['date']}\n"

    if ai_result.get("people"):
        text += f"👥 {', '.join(ai_result['people'])}\n"

    # Preview of original message
    preview = msg.content[:300]
    if len(msg.content) > 300:
        preview += "..."
    text += f"\n{preview}"

    silent = priority != "high"
    markup = task_keyboard(task.id) if task else None

    try:
        await bot.send_message(
            chat_id=settings.telegram_owner_id,
            text=text,
            parse_mode="HTML",
            disable_notification=silent,
            reply_markup=markup,
        )
        # Update status to notified
        async with async_session() as session:
            msg_obj = await session.get(Message, msg.id)
            if msg_obj:
                msg_obj.status = "notified"
                msg_obj.notified_at = func.now()
                await session.commit()
    except Exception:
        logger.exception(f"Failed to notify about message {msg.id}")


async def create_task_if_needed(msg: Message, ai_result: dict) -> Task | None:
    """Create a Task record if AI categorized the message as task or deadline."""
    category = ai_result.get("category")
    if category not in ("task", "deadline"):
        return None

    topic = ai_result.get("topic") or msg.content[:200]
    due_date = None
    if ai_result.get("date"):
        try:
            due_date = datetime.fromisoformat(ai_result["date"])
        except (ValueError, TypeError):
            pass

    async with async_session() as session:
        task = Task(
            message_id=msg.id,
            title=topic,
            due_date=due_date,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def process_batch(message_ids: list[int], bot: Bot | None = None) -> None:
    """Run L1 filter, then L2 AI on a batch of messages."""
    async with async_session() as session:
        result = await session.execute(
            select(Message).where(Message.id.in_(message_ids)).where(Message.status.in_(["raw", "processing"]))
        )
        messages = list(result.scalars().all())

    # L1 filter
    to_ai = []
    for msg in messages:
        msg_dict = {"content": msg.content, "content_type": msg.content_type}
        if should_process(msg_dict):
            to_ai.append(msg)
        else:
            async with async_session() as session:
                msg_obj = await session.get(Message, msg.id)
                if msg_obj:
                    msg_obj.status = "processed"
                    msg_obj.category = "noise"
                    await session.commit()

    if not to_ai:
        return

    # L2 AI categorization
    ai_input = [
        {
            "id": msg.id,
            "chat": msg.source_chat or "",
            "sender": msg.sender or "",
            "text": msg.content,
            "reply_to": msg.reply_to_text,
            "type": msg.content_type,
        }
        for msg in to_ai
    ]

    try:
        provider = get_primary_provider()
        results = await provider.categorize(ai_input)
    except Exception:
        logger.warning("Primary AI provider failed, trying fallback")
        try:
            provider = get_fallback_provider()
            results = await provider.categorize(ai_input)
        except Exception:
            logger.exception("All AI providers failed, marking as pending_ai")
            async with async_session() as session:
                for msg in to_ai:
                    msg_obj = await session.get(Message, msg.id)
                    if msg_obj:
                        msg_obj.status = "pending_ai"
                await session.commit()
            return

    # Save AI results + notify + create tasks
    result_map = {r.get("id"): r for r in results if "id" in r}
    for msg in to_ai:
        ai_result = result_map.get(msg.id, {})

        # Save to DB
        async with async_session() as session:
            msg_obj = await session.get(Message, msg.id)
            if msg_obj:
                msg_obj.category = ai_result.get("category")
                msg_obj.priority = ai_result.get("priority")
                msg_obj.extracted_topic = ai_result.get("topic")
                msg_obj.extracted_people = ai_result.get("people")
                msg_obj.ai_response = ai_result
                msg_obj.status = "processed"

                if ai_result.get("date"):
                    try:
                        msg_obj.extracted_date = datetime.fromisoformat(ai_result["date"])
                    except (ValueError, TypeError):
                        pass
                await session.commit()

        # Create task if needed
        task = await create_task_if_needed(msg, ai_result)

        # Notify owner
        if bot and ai_result.get("category"):
            await notify_owner(bot, msg, ai_result, task)


async def message_processor(queue: asyncio.Queue, bot: Bot | None = None) -> None:
    """Main processor loop. Queue contains message IDs (already persisted in DB)."""
    batch: list[int] = []

    while True:
        try:
            # Wait for a message ID from queue
            try:
                msg_id = await asyncio.wait_for(queue.get(), timeout=BATCH_TIMEOUT)
                batch.append(msg_id)

                # Drain remaining without waiting
                while not queue.empty():
                    batch.append(queue.get_nowait())
            except asyncio.TimeoutError:
                pass

            # Process in batches of BATCH_SIZE
            while batch:
                current_batch = batch[:BATCH_SIZE]
                batch = batch[BATCH_SIZE:]
                await process_batch(current_batch, bot=bot)

        except asyncio.CancelledError:
            if batch:
                await process_batch(batch, bot=bot)
            raise
        except Exception:
            logger.exception("Error in message processor")
            await asyncio.sleep(1)


async def requeue_pending() -> list[int]:
    """On startup: re-queue only pending_ai (already passed L1). Raw will be picked up by background worker."""
    async with async_session() as session:
        result = await session.execute(
            select(Message.id).where(Message.status == "pending_ai").order_by(Message.created_at).limit(20)
        )
        return list(result.scalars().all())


async def process_raw_backlog(bot: Bot | None = None) -> None:
    """Background: process old raw messages in batches. Claim with status='processing' to avoid races."""
    async with async_session() as session:
        # SELECT FOR UPDATE SKIP LOCKED — no two jobs pick the same rows
        result = await session.execute(
            select(Message)
            .where(Message.status == "raw")
            .order_by(Message.created_at.asc())
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        messages = list(result.scalars().all())
        if not messages:
            return
        for msg in messages:
            msg.status = "processing"
        await session.commit()
        ids = [msg.id for msg in messages]

    if ids:
        logger.info(f"Processing {len(ids)} raw backlog messages")
        await process_batch(ids, bot=bot)
