import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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


@dataclass
class NotificationItem:
    msg: Message
    ai_result: dict
    task: Task | None
    icon: str
    topic: str
    sender: str
    time_str: str
    priority: str


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

    # Save results + create tasks + collect notifications
    result_map = {r.get("id"): r for r in results if "id" in r}
    notification_buffer: list[NotificationItem] = []

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

        task = await _create_task(msg, ai_result)

        if bot:
            item = await _prepare_notification(msg, ai_result, task)
            if item:
                notification_buffer.append(item)

    if bot and notification_buffer:
        await _flush_notifications(bot, notification_buffer)

    # Invalidate digest cache when new messages processed
    from src.app.scheduler.jobs import _digest_cache
    _digest_cache.clear()


async def _create_task(msg: Message, ai_result: dict) -> Task | None:
    category = ai_result.get("category")
    if category not in ("task", "deadline", "meeting"):
        return None

    topic = ai_result.get("topic") or msg.content[:200]
    due_date = None
    if ai_result.get("date"):
        due_date = _parse_ai_date(ai_result["date"])

    # Dedup: don't create if similar active task exists
    async with async_session() as session:
        existing = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .where(func.lower(Task.title) == topic.strip().lower())
        )
        if existing.scalar_one_or_none():
            return None

        task = Task(message_id=msg.id, title=topic, due_date=due_date)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def _is_quiet_or_muted() -> bool:
    """Check if quiet hours are active or bot is muted."""
    from src.app.db.models import UserSettings
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == settings.telegram_owner_id)
        )
        us = result.scalar_one_or_none()
        if not us:
            return False

        # Check mute
        muted_until = (us.quiet_hours or {}).get("muted_until")
        if muted_until:
            mute_dt = datetime.fromisoformat(muted_until)
            if datetime.now(USER_TZ) < mute_dt:
                return True

        # Check quiet hours
        start = (us.quiet_hours or {}).get("start")
        end = (us.quiet_hours or {}).get("end")
        if start and end:
            now_time = datetime.now(USER_TZ).strftime("%H:%M")
            # Handle overnight ranges (e.g. 22:00-08:00)
            if start > end:
                if now_time >= start or now_time < end:
                    return True
            else:
                if start <= now_time < end:
                    return True

    return False


async def _prepare_notification(msg: Message, ai_result: dict, task: Task | None = None) -> NotificationItem | None:
    category = ai_result.get("category", "info")
    priority = ai_result.get("priority", "low")

    if category == "noise":
        return None
    if category == "info" and priority == "low":
        return None

    quiet = await _is_quiet_or_muted()

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
            return None

        if quiet:
            msg_obj.status = "processed"
            await session.commit()
            return None

        msg_obj.status = "notified"
        msg_obj.notified_at = func.now()
        await session.commit()

    icon = CATEGORY_ICONS.get(category, "💬")
    topic = ai_result.get("topic") or msg.content[:100]
    sender = msg.sender or ""
    time_str = msg.created_at.strftime("%H:%M") if msg.created_at else ""

    return NotificationItem(
        msg=msg, ai_result=ai_result, task=task,
        icon=icon, topic=topic, sender=sender,
        time_str=time_str, priority=priority,
    )


def _format_single(item: NotificationItem) -> str:
    chat = item.msg.source_chat or "—"
    text = f"{item.icon} <b>{item.topic}</b>\n"
    text += f"<i>{chat} — {item.sender}</i>\n"
    if item.ai_result.get("date"):
        text += f"📆 {_format_date(item.ai_result['date'])}\n"
    if item.ai_result.get("people"):
        text += f"👥 {', '.join(item.ai_result['people'])}\n"
    preview = item.msg.content[:300]
    if len(item.msg.content) > 300:
        preview += "..."
    text += f"\n{preview}"
    return text


def _format_grouped(chat_name: str, items: list[NotificationItem]) -> str:
    count = len(items)
    noun = "повідомлення" if 2 <= count <= 4 else "повідомлень"
    text = f"<b>{chat_name}</b> • {count} {noun}\n"
    for item in items:
        text += f"\n{item.icon} {item.topic}\n"
        text += f"    <i>{item.sender} • {item.time_str}</i>\n"
    return text


async def _flush_notifications(bot: Bot, buffer: list[NotificationItem]) -> None:
    groups: dict[str, list[NotificationItem]] = defaultdict(list)
    for item in buffer:
        key = item.msg.source_chat or "—"
        groups[key].append(item)

    for chat_name, items in groups.items():
        has_task = any(i.task for i in items)
        is_high = any(i.priority == "high" for i in items)

        if len(items) == 1:
            item = items[0]
            text = _format_single(item)
            reply_markup = task_keyboard(item.task.id) if item.task else None
        else:
            text = _format_grouped(chat_name, items)
            if has_task:
                task_items = [i for i in items if i.task]
                buttons = []
                for ti in task_items:
                    short = ti.topic[:20] + ("..." if len(ti.topic) > 20 else "")
                    buttons.append([
                        InlineKeyboardButton(text=f"✅ {short}", callback_data=f"task_done:{ti.task.id}"),
                        InlineKeyboardButton(text="⏰ 1h", callback_data=f"task_snooze:{ti.task.id}:1"),
                    ])
                reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
            else:
                reply_markup = None

        try:
            await bot.send_message(
                chat_id=settings.telegram_owner_id,
                text=text,
                parse_mode="HTML",
                disable_notification=(not is_high),
                reply_markup=reply_markup,
            )
        except Exception:
            msg_ids = [i.msg.id for i in items]
            logger.exception(f"Failed to send grouped notification for messages {msg_ids}")


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
