import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.app.db.models import Message, Task
from src.app.db.session import async_session
from src.app.processors.ai_provider import get_fallback_provider, get_primary_provider
from src.app.processors.filter_l1 import should_process
from src.app.bot.keyboards import task_keyboard
from src.app.config import settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 10

USER_TZ = ZoneInfo("Europe/Kyiv")
DAYS_UK = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]


def _parse_ai_date(iso_str: str) -> datetime | None:
    """Parse AI date string. Treat naive datetimes as user timezone (Kyiv)."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=USER_TZ)
        return dt
    except (ValueError, TypeError):
        return None


def _format_date(iso_str: str) -> str:
    """'2026-04-02T15:00:00' → 'завтра о 15:00' or 'середа, 02.04 о 15:00'."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=USER_TZ)
        now = datetime.now(dt.tzinfo)
        today = now.date()
        target = dt.date()
        delta = (target - today).days

        time_str = dt.strftime("%H:%M")

        if delta == 0:
            return f"сьогодні о {time_str}"
        elif delta == 1:
            return f"завтра о {time_str}"
        elif delta == -1:
            return f"вчора о {time_str}"
        elif 2 <= delta <= 6:
            day_name = DAYS_UK[dt.weekday()]
            return f"{day_name}, {dt.strftime('%d.%m')} о {time_str}"
        else:
            return f"{dt.strftime('%d.%m.%Y')} о {time_str}"
    except (ValueError, TypeError):
        return iso_str


CATEGORY_ICONS = {
    "meeting": "📅",
    "task": "📋",
    "deadline": "🔴",
    "info": "💡",
}


async def persist_raw(message_data: dict) -> int | None:
    """Write-ahead: save to DB immediately. Returns message ID."""
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
        return result.scalar_one_or_none()


async def claim_raw_messages(limit: int = BATCH_SIZE) -> list[Message]:
    """Atomically claim raw messages for processing. Returns claimed messages."""
    async with async_session() as session:
        # Subquery to get IDs, then update atomically
        subq = (
            select(Message.id)
            .where(Message.status == "raw")
            .order_by(Message.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(select(Message).where(Message.id.in_(subq)))
        messages = list(result.scalars().all())

        if not messages:
            return []

        ids = [m.id for m in messages]
        await session.execute(
            update(Message).where(Message.id.in_(ids)).values(status="processing")
        )
        await session.commit()
        return messages


async def process_messages(messages: list[Message], bot: Bot | None = None) -> None:
    """Process a batch: L1 filter → L2 AI → save → notify."""
    if not messages:
        return

    # L1 filter
    to_ai = []
    noise_ids = []
    for msg in messages:
        if should_process({"content": msg.content, "content_type": msg.content_type}):
            to_ai.append(msg)
        else:
            noise_ids.append(msg.id)

    # Mark noise
    if noise_ids:
        async with async_session() as session:
            await session.execute(
                update(Message).where(Message.id.in_(noise_ids)).values(status="processed", category="noise")
            )
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
        results = await get_primary_provider().categorize(ai_input)
    except Exception:
        logger.warning("Primary AI failed, trying fallback")
        try:
            results = await get_fallback_provider().categorize(ai_input)
        except Exception:
            logger.exception("All AI providers failed")
            async with async_session() as session:
                await session.execute(
                    update(Message).where(Message.id.in_([m.id for m in to_ai])).values(status="pending_ai")
                )
                await session.commit()
            return

    # Save results + create tasks + notify
    result_map = {r.get("id"): r for r in results if "id" in r}

    for msg in to_ai:
        ai_result = result_map.get(msg.id, {})
        if not ai_result:
            continue

        # Save AI result
        async with async_session() as session:
            msg_obj = await session.get(Message, msg.id)
            if not msg_obj or msg_obj.status in ("processed", "notified"):
                continue  # Already done by another worker

            msg_obj.category = ai_result.get("category")
            msg_obj.priority = ai_result.get("priority")
            msg_obj.extracted_topic = ai_result.get("topic")
            msg_obj.extracted_people = ai_result.get("people")
            msg_obj.ai_response = ai_result
            msg_obj.status = "processed"

            if ai_result.get("date"):
                msg_obj.extracted_date = _parse_ai_date(ai_result["date"])
            await session.commit()

        # Create task if needed
        task = await _create_task(msg, ai_result)

        # Notify (only once — status check inside)
        if bot:
            await _notify(bot, msg, ai_result, task)


async def _create_task(msg: Message, ai_result: dict) -> Task | None:
    category = ai_result.get("category")
    if category not in ("task", "deadline", "meeting"):
        return None

    topic = ai_result.get("topic") or msg.content[:200]
    due_date = None
    if ai_result.get("date"):
        due_date = _parse_ai_date(ai_result["date"])

    async with async_session() as session:
        task = Task(message_id=msg.id, title=topic, due_date=due_date)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def _notify(bot: Bot, msg: Message, ai_result: dict, task: Task | None = None) -> None:
    """Send notification. Checks status to prevent duplicates."""
    category = ai_result.get("category", "info")
    priority = ai_result.get("priority", "low")

    if category == "noise":
        return
    if category == "info" and priority == "low":
        return

    # Atomic check-and-set to prevent duplicate notifications
    async with async_session() as session:
        fresh = await session.execute(
            select(Message)
            .where(Message.id == msg.id)
            .where(Message.status != "notified")
            .with_for_update()
        )
        msg_obj = fresh.scalar_one_or_none()
        if not msg_obj:
            return  # Already notified

        msg_obj.status = "notified"
        msg_obj.notified_at = func.now()
        await session.commit()

    icon = CATEGORY_ICONS.get(category, "💬")
    topic = ai_result.get("topic") or msg.content[:100]
    chat = msg.source_chat or "—"
    sender = msg.sender or ""

    text = f"{icon} <b>{topic}</b>\n"
    text += f"<i>{chat} — {sender}</i>\n"

    if ai_result.get("date"):
        text += f"📆 {_format_date(ai_result['date'])}\n"
    if ai_result.get("people"):
        text += f"👥 {', '.join(ai_result['people'])}\n"

    preview = msg.content[:300]
    if len(msg.content) > 300:
        preview += "..."
    text += f"\n{preview}"

    try:
        await bot.send_message(
            chat_id=settings.telegram_owner_id,
            text=text,
            parse_mode="HTML",
            disable_notification=(priority != "high"),
            reply_markup=task_keyboard(task.id) if task else None,
        )
    except Exception:
        logger.exception(f"Failed to send notification for message {msg.id}")


# ── Main processor loop ──────────────────────────────────────

async def message_processor(queue: asyncio.Queue, bot: Bot | None = None) -> None:
    """Listens to queue for new message IDs, processes in batches."""
    batch: list[int] = []

    while True:
        try:
            try:
                msg_id = await asyncio.wait_for(queue.get(), timeout=5)
                batch.append(msg_id)
                while not queue.empty():
                    batch.append(queue.get_nowait())
            except asyncio.TimeoutError:
                pass

            if batch:
                # Fetch fresh from DB (already persisted by Telethon handler)
                async with async_session() as session:
                    result = await session.execute(
                        select(Message)
                        .where(Message.id.in_(batch))
                        .where(Message.status == "raw")
                    )
                    messages = list(result.scalars().all())
                batch.clear()

                if messages:
                    # Mark as processing
                    async with async_session() as session:
                        await session.execute(
                            update(Message)
                            .where(Message.id.in_([m.id for m in messages]))
                            .values(status="processing")
                        )
                        await session.commit()
                    await process_messages(messages, bot)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in message processor")
            batch.clear()
            await asyncio.sleep(1)


async def process_raw_backlog(bot: Bot | None = None) -> None:
    """Scheduled job: picks up raw messages missed by the queue path."""
    messages = await claim_raw_messages(BATCH_SIZE)
    if messages:
        logger.info(f"Backlog: processing {len(messages)} messages")
        await process_messages(messages, bot)


async def requeue_pending() -> list[int]:
    """On startup: re-queue only pending_ai messages."""
    async with async_session() as session:
        result = await session.execute(
            select(Message.id).where(Message.status == "pending_ai").order_by(Message.created_at).limit(20)
        )
        return list(result.scalars().all())
