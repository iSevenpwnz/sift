import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.app.db.models import Message
from src.app.db.session import async_session
from src.app.processors.ai_provider import get_fallback_provider, get_primary_provider
from src.app.processors.filter_l1 import should_process

logger = logging.getLogger(__name__)

BATCH_SIZE = 5
BATCH_TIMEOUT = 30  # seconds


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
                set_={"content": message_data["content"], "updated_at": Message.updated_at.default.arg},
            )
            .returning(Message.id)
        )
        result = await session.execute(stmt)
        await session.commit()
        row = result.scalar_one_or_none()
        return row


async def process_batch(message_ids: list[int]) -> None:
    """Run L1 filter, then L2 AI on a batch of messages."""
    async with async_session() as session:
        result = await session.execute(select(Message).where(Message.id.in_(message_ids)))
        messages = list(result.scalars().all())

    # L1 filter
    to_ai = []
    for msg in messages:
        msg_dict = {"content": msg.content, "content_type": msg.content_type}
        if should_process(msg_dict):
            to_ai.append(msg)
        else:
            # Mark as processed (noise)
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

    # Save AI results to DB
    result_map = {r.get("id"): r for r in results if "id" in r}
    async with async_session() as session:
        for msg in to_ai:
            ai_result = result_map.get(msg.id, {})
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
                        from datetime import datetime

                        msg_obj.extracted_date = datetime.fromisoformat(ai_result["date"])
                    except (ValueError, TypeError):
                        pass
        await session.commit()


async def message_processor(queue: asyncio.Queue) -> None:
    """Main processor loop. Batches messages from queue and processes them."""
    batch: list[int] = []

    while True:
        try:
            # Collect up to BATCH_SIZE messages or wait BATCH_TIMEOUT
            try:
                msg_data = await asyncio.wait_for(queue.get(), timeout=BATCH_TIMEOUT)
                msg_id = await persist_raw(msg_data)
                if msg_id:
                    batch.append(msg_id)
            except asyncio.TimeoutError:
                pass  # Timeout reached, process what we have

            # Process batch when full or on timeout
            if len(batch) >= BATCH_SIZE or (batch and queue.empty()):
                current_batch = batch[:BATCH_SIZE]
                batch = batch[BATCH_SIZE:]
                await process_batch(current_batch)

        except asyncio.CancelledError:
            # Process remaining batch on shutdown
            if batch:
                await process_batch(batch)
            raise
        except Exception:
            logger.exception("Error in message processor")
            await asyncio.sleep(1)


async def requeue_pending() -> list[int]:
    """On startup: find messages stuck in 'raw' or 'pending_ai' and return their IDs."""
    async with async_session() as session:
        result = await session.execute(
            select(Message.id).where(Message.status.in_(["raw", "pending_ai"])).order_by(Message.created_at)
        )
        return list(result.scalars().all())
