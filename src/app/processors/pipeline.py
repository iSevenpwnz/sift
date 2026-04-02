import asyncio
import html as html_mod
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.app.db.models import ChatDailySummary, Message, Task, UserSettings
from src.app.db.session import async_session
from src.app.constants import CATEGORY_ICONS
from src.app.processors.ai_provider import get_fallback_provider, get_primary_provider
from src.app.processors.filter_l1 import should_process
from src.app.bot.keyboards import task_keyboard
from src.app.config import settings

SUMMARY_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "update_summary.txt"

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
        subq = (
            select(Message.id)
            .where(Message.status == "raw")
            .order_by(Message.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        await session.execute(
            update(Message).where(Message.id.in_(subq)).values(status="processing")
        )
        result = await session.execute(
            select(Message)
            .where(Message.status == "processing")
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        messages = list(result.scalars().all())
        await session.commit()
        return messages


async def update_chat_summaries(messages: list[Message], result_map: dict) -> None:
    """Incrementally update daily chat summaries for non-noise messages."""
    try:
        today = date.today()
        non_noise = [
            m for m in messages
            if result_map.get(m.id, {}).get("category") not in (None, "noise")
        ]
        logger.info(f"update_chat_summaries: {len(messages)} msgs, {len(non_noise)} non-noise")
        if not non_noise:
            return

        groups: dict[str, list[Message]] = defaultdict(list)
        for msg in non_noise:
            chat = msg.source_chat or "Unknown"
            groups[chat].append(msg)

        template = SUMMARY_PROMPT_PATH.read_text() if SUMMARY_PROMPT_PATH.exists() else ""
        if not template:
            return

        async with async_session() as session:
            existing_result = await session.execute(
                select(ChatDailySummary).where(
                    ChatDailySummary.summary_date == today,
                    ChatDailySummary.chat_name.in_(list(groups.keys())),
                )
            )
            existing = {s.chat_name: s for s in existing_result.scalars().all()}

        provider = get_primary_provider()

        for chat_name, msgs in groups.items():
            try:
                current = existing.get(chat_name)
                current_summary = current.summary_text if current else ""
                current_count = current.message_count if current else 0

                new_messages = "\n".join(
                    f"- {m.sender or 'Unknown'}: {m.content[:150]}" for m in msgs
                )

                prompt = template.replace("{current_summary}", current_summary).replace("{new_messages}", new_messages)

                response = await asyncio.wait_for(
                    provider.client.chat.completions.create(
                        model=provider.model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=200,
                        temperature=0.3,
                    ),
                    timeout=25,
                )
                summary_text = response.choices[0].message.content or ""

                async with async_session() as session:
                    stmt = (
                        pg_insert(ChatDailySummary)
                        .values(
                            chat_name=chat_name,
                            summary_date=today,
                            summary_text=summary_text.strip(),
                            message_count=current_count + len(msgs),
                        )
                        .on_conflict_do_update(
                            constraint="uq_chat_daily_summary",
                            set_={
                                "summary_text": summary_text.strip(),
                                "message_count": current_count + len(msgs),
                                "last_updated": func.now(),
                            },
                        )
                    )
                    await session.execute(stmt)
                    await session.commit()

            except Exception:
                logger.warning(f"Failed to update summary for chat {chat_name}", exc_info=True)

    except Exception:
        logger.warning("Failed to update chat summaries", exc_info=True)


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

    # Load chat context summaries for today
    today = date.today()
    chat_names = list({m.source_chat for m in to_ai if m.source_chat})
    chat_context_map: dict[str, str] = {}
    if chat_names:
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(ChatDailySummary).where(
                        ChatDailySummary.summary_date == today,
                        ChatDailySummary.chat_name.in_(chat_names),
                    )
                )
                chat_context_map = {s.chat_name: s.summary_text for s in result.scalars().all()}
        except Exception:
            logger.warning("Failed to load chat summaries for context", exc_info=True)

    # Load important_people from user settings
    important_people = None
    try:
        async with async_session() as session:
            result = await session.execute(
                select(UserSettings).where(UserSettings.telegram_user_id == settings.telegram_owner_id)
            )
            us = result.scalar_one_or_none()
            if us and us.important_people:
                important_people = us.important_people
    except Exception:
        logger.warning("Failed to load important_people", exc_info=True)

    # L2 AI categorization
    ai_input = [
        {
            "id": msg.id,
            "chat": msg.source_chat or "",
            "chat_context": chat_context_map.get(msg.source_chat or "", ""),
            "sender": msg.sender or "",
            "text": msg.content,
            "reply_to": msg.reply_to_text,
            "type": msg.content_type,
        }
        for msg in to_ai
    ]

    try:
        results = await get_primary_provider().categorize(ai_input, important_people)
    except Exception:
        logger.warning("Primary AI failed, trying fallback")
        try:
            results = await get_fallback_provider().categorize(ai_input, important_people)
        except Exception:
            logger.exception("All AI providers failed")
            async with async_session() as session:
                await session.execute(
                    update(Message).where(Message.id.in_([m.id for m in to_ai])).values(status="pending_ai")
                )
                await session.commit()
            return

    result_map = {r.get("id"): r for r in results if "id" in r}
    notification_buffer: list[NotificationItem] = []

    async with async_session() as session:
        msg_ids = [m.id for m in to_ai if m.id in result_map]
        result = await session.execute(
            select(Message).where(Message.id.in_(msg_ids))
        )
        msg_objs = {m.id: m for m in result.scalars().all()}

        for msg in to_ai:
            ai_result = result_map.get(msg.id, {})
            if not ai_result:
                continue

            msg_obj = msg_objs.get(msg.id)
            if not msg_obj or msg_obj.status in ("processed", "notified"):
                continue

            msg_obj.category = ai_result.get("category")
            msg_obj.priority = ai_result.get("priority")
            msg_obj.extracted_topic = ai_result.get("topic")
            msg_obj.extracted_people = ai_result.get("people")
            msg_obj.ai_response = ai_result
            msg_obj.status = "processed"

            if ai_result.get("date"):
                msg_obj.extracted_date = _parse_ai_date(ai_result["date"])

        await session.commit()

    await update_chat_summaries(to_ai, result_map)

    for msg in to_ai:
        ai_result = result_map.get(msg.id, {})
        if not ai_result:
            continue

        task = await _create_task(msg, ai_result)
        await _create_reminder(msg, ai_result)

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

    # Dedup: don't create if similar active task exists (fuzzy — first 20 chars)
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.is_done.is_(False))
        )
        active_tasks = list(result.scalars().all())
        topic_key = topic.strip().lower()[:20]
        for t in active_tasks:
            if t.title.strip().lower()[:20] == topic_key:
                return None

        task = Task(message_id=msg.id, title=topic, due_date=due_date)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def _create_reminder(msg: Message, ai_result: dict) -> None:
    """Create a Reminder if AI suggests one."""
    reminder_str = ai_result.get("reminder")
    if not reminder_str:
        return

    remind_at = _parse_ai_date(reminder_str)
    if not remind_at:
        return

    # Don't create reminders in the past
    if remind_at <= datetime.now(USER_TZ):
        return

    try:
        from src.app.db.models import Reminder
        async with async_session() as session:
            # Dedup: don't create if similar reminder exists
            existing = await session.execute(
                select(Reminder)
                .where(Reminder.message_id == msg.id)
                .where(Reminder.sent.is_(False))
            )
            if existing.scalar_one_or_none():
                return

            reminder = Reminder(message_id=msg.id, remind_at=remind_at)
            session.add(reminder)
            await session.commit()
            logger.info(f"Created reminder for message {msg.id} at {remind_at}")
    except Exception:
        logger.warning(f"Failed to create reminder for message {msg.id}", exc_info=True)


async def _is_quiet_or_muted() -> bool:
    """Check if quiet hours are active or bot is muted."""
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

    # info/low from large channels — skip (noise-like)
    if category == "info" and priority == "low":
        meta = msg.raw_metadata or {}
        chat_id = meta.get("chat_id", 0)
        is_dm = isinstance(chat_id, int) and chat_id > 0
        # DMs and personal groups — still notify silently
        if not is_dm and not (msg.sender and msg.sender != "Unknown"):
            return None  # channel info/low — skip

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


# Cache: chat_name → (bot_message_id, list of NotificationItems sent today)
_notification_cache: dict[str, tuple[int, list[NotificationItem]]] = {}


async def _format_summarized(chat_name: str, items: list[NotificationItem]) -> str:
    """Format notification using chat daily summary from DB."""
    from src.app.db.models import ChatDailySummary

    e = html_mod.escape
    count = len(items)
    noun = "повідомлення" if 2 <= count <= 4 else "повідомлень" if count >= 5 else "повідомлення"

    # Load summary from DB
    summary = ""
    try:
        today = date.today()
        async with async_session() as session:
            result = await session.execute(
                select(ChatDailySummary)
                .where(ChatDailySummary.chat_name == chat_name)
                .where(ChatDailySummary.summary_date == today)
            )
            row = result.scalar_one_or_none()
            if row:
                summary = row.summary_text
    except Exception:
        pass

    text = f"<b>{e(chat_name)}</b> • {count} {noun}\n\n"

    if summary:
        text += f"<blockquote expandable>{e(summary)}</blockquote>\n"
    else:
        # Fallback: list topics
        for item in items[-5:]:  # last 5
            text += f"{item.icon} {e(item.topic)}\n"
            text += f"    <i>{e(item.sender)} • {item.time_str}</i>\n"

    # Action items at the bottom
    actions = [i for i in items if i.ai_result.get("category") in ("meeting", "task", "deadline")]
    if actions:
        seen = set()
        for a in actions:
            key = a.topic[:20].lower()
            if key in seen:
                continue
            seen.add(key)
            text += f"\n{a.icon} {e(a.topic)}"
            if a.ai_result.get("date"):
                text += f" — {_format_date(a.ai_result['date'])}"

    return text


async def _flush_notifications(bot: Bot, buffer: list[NotificationItem]) -> None:
    from src.app.bot.keyboards import notification_keyboard

    groups: dict[str, list[NotificationItem]] = defaultdict(list)
    for item in buffer:
        key = item.msg.source_chat or "—"
        groups[key].append(item)

    for chat_name, new_items in groups.items():
        is_high = any(i.priority == "high" for i in new_items)

        # Check if we already have a notification for this chat — edit it
        cached = _notification_cache.get(chat_name)
        if cached:
            bot_msg_id, prev_items = cached
            all_items = prev_items + new_items

            # 3+ items → use AI summary instead of individual list
            if len(all_items) >= 3:
                text = await _format_summarized(chat_name, all_items)
            else:
                text = _format_grouped(chat_name, all_items)

            try:
                await bot.edit_message_text(
                    chat_id=settings.telegram_owner_id,
                    message_id=bot_msg_id,
                    text=text,
                    parse_mode="HTML",
                )
                _notification_cache[chat_name] = (bot_msg_id, all_items)
                continue
            except Exception:
                pass  # Edit failed (too old, deleted) — send new

        # Build text
        if len(new_items) == 1:
            item = new_items[0]
            text = _format_single(item)
            if item.task:
                reply_markup = task_keyboard(item.task.id, msg_id=item.msg.id)
            else:
                reply_markup = notification_keyboard(item.msg.id)
        else:
            text = _format_grouped(chat_name, new_items)
            reply_markup = None

        try:
            sent = await bot.send_message(
                chat_id=settings.telegram_owner_id,
                text=text,
                parse_mode="HTML",
                disable_notification=(not is_high),
                reply_markup=reply_markup,
            )
            _notification_cache[chat_name] = (sent.message_id, new_items)
        except Exception:
            msg_ids = [i.msg.id for i in new_items]
            logger.exception(f"Failed to send notification for messages {msg_ids}")


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
