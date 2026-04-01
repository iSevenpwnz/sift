import logging
from collections import defaultdict
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select

from src.app.db.models import Message, Task
from src.app.db.session import async_session
from src.app.config import settings
from src.app.bot.keyboards import task_keyboard
from src.app.processors.pipeline import process_messages
from src.app.processors.ai_provider import get_primary_provider, get_fallback_provider

logger = logging.getLogger(__name__)

USER_TZ = ZoneInfo("Europe/Kyiv")

CATEGORY_ICONS = {
    "meeting": "📅",
    "task": "📋",
    "deadline": "🔴",
    "info": "💡",
}


async def _summarize_groups(groups: dict[str, list[Message]]) -> dict[str, str]:
    """Ask AI to summarize each chat group based on full message content."""
    items = []
    for chat_name, msgs in groups.items():
        # Give AI the actual messages, not just topics
        messages_text = []
        for m in msgs[:15]:  # max 15 per chat
            sender = m.sender or ""
            text = m.content[:300]
            if sender:
                messages_text.append(f"{sender}: {text}")
            else:
                messages_text.append(text)
        items.append({
            "chat": chat_name,
            "count": len(msgs),
            "messages": messages_text,
        })

    if not items:
        return {}

    import json
    from pathlib import Path

    prompt_path = Path(__file__).parent.parent.parent.parent / "prompts" / "digest_summary.txt"
    system_prompt = prompt_path.read_text() if prompt_path.exists() else "Summarize each chat in Ukrainian. Return JSON."

    user_content = json.dumps(items, ensure_ascii=False)

    try:
        provider = get_primary_provider()
        response = await provider.client.chat.completions.create(
            model=provider.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        text = response.choices[0].message.content or "{}"
        parsed = json.loads(text)
        return parsed.get("summaries", {})
    except Exception:
        logger.warning("Failed to generate digest summaries, using fallback")
        # Fallback: just concatenate topics
        result = {}
        for chat_name, msgs in groups.items():
            topics = [m.extracted_topic or m.content[:40] for m in msgs[:5]]
            result[chat_name] = "; ".join(t for t in topics if t)
        return result


def _digest_nav_keyboard(target_date: date) -> InlineKeyboardMarkup:
    """Navigation buttons for digest: ◀️ Prev day / Next day ▶️"""
    prev_date = target_date - timedelta(days=1)
    next_date = target_date + timedelta(days=1)
    today = datetime.now(USER_TZ).date()

    buttons = [InlineKeyboardButton(text=f"◀️ {prev_date.strftime('%d.%m')}", callback_data=f"digest:{prev_date.isoformat()}")]
    if target_date < today:
        buttons.append(InlineKeyboardButton(text=f"{next_date.strftime('%d.%m')} ▶️", callback_data=f"digest:{next_date.isoformat()}"))

    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def build_digest(target_date: date) -> tuple[str, InlineKeyboardMarkup]:
    """Build digest text for a specific date. Returns (text, keyboard)."""
    async with async_session() as session:
        # All non-noise messages for this date, grouped by chat
        result = await session.execute(
            select(Message)
            .where(func.date(Message.created_at) == target_date)
            .where(Message.category.is_not(None))
            .where(Message.category != "noise")
            .where(Message.source_chat.is_not(None))
            .order_by(Message.created_at.desc())
        )
        all_msgs = list(result.scalars().all())

        # Stats
        result = await session.execute(
            select(func.count()).select_from(Message)
            .where(func.date(Message.created_at) == target_date)
            .where(Message.category.is_not(None))
        )
        total = result.scalar_one()

        result = await session.execute(
            select(func.count()).select_from(Message)
            .where(func.date(Message.created_at) == target_date)
            .where(Message.category == "noise")
        )
        noise = result.scalar_one()

        # Active tasks
        result = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .where((Task.snoozed_until.is_(None)) | (Task.snoozed_until <= func.now()))
            .order_by(Task.due_date.asc().nulls_last())
            .limit(10)
        )
        tasks = list(result.scalars().all())

    if not all_msgs and not tasks:
        date_str = target_date.strftime("%d.%m")
        return f"📊 <b>Дайджест за {date_str}</b>\n\nНічого важливого.", _digest_nav_keyboard(target_date)

    # Group by chat
    chat_groups: dict[str, list[Message]] = defaultdict(list)
    for msg in all_msgs:
        chat_groups[msg.source_chat or "Інше"].append(msg)

    # Get AI summaries
    summaries = await _summarize_groups(chat_groups)

    # Build text
    date_str = target_date.strftime("%d.%m")
    useful = total - noise
    lines = [f"📊 <b>Дайджест за {date_str}</b>  •  {useful} корисних, {noise} шум\n"]

    for chat_name, msgs in sorted(chat_groups.items(), key=lambda x: -len(x[1])):
        count = len(msgs)
        noun = "повідомлення" if 2 <= count <= 4 else "повідомлень" if count >= 5 else "повідомлення"

        # Chat header
        lines.append(f"💬 <b>{chat_name}</b> — {count} {noun}")

        # AI summary as blockquote
        summary = summaries.get(chat_name, "")
        if summary:
            lines.append(f"<blockquote>{summary}</blockquote>")

        # Meetings/tasks/deadlines from this chat
        for msg in msgs:
            if msg.category in ("meeting", "deadline", "task"):
                icon = CATEGORY_ICONS.get(msg.category, "📌")
                topic = msg.extracted_topic or msg.content[:60]
                date_info = ""
                if msg.extracted_date:
                    date_info = f" — {msg.extracted_date.strftime('%d.%m %H:%M')}"
                lines.append(f"  {icon} {topic}{date_info}")

        lines.append("")

    # Tasks
    if tasks:
        lines.append(f"📋 <b>Активні таски ({len(tasks)}):</b>")
        for task in tasks:
            due = f" • до {task.due_date.strftime('%d.%m %H:%M')}" if task.due_date else ""
            lines.append(f"  • {task.title}{due}")
    else:
        lines.append("📋 Активних тасків: 0")

    text = "\n".join(lines)

    # Trim if too long (Telegram limit 4096)
    if len(text) > 4000:
        text = text[:3990] + "\n\n<i>...обрізано</i>"

    return text, _digest_nav_keyboard(target_date)


async def daily_digest(bot: Bot) -> None:
    """Send daily digest to owner."""
    today = datetime.now(USER_TZ).date()
    text, keyboard = await build_digest(today)
    await bot.send_message(
        chat_id=settings.telegram_owner_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
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


async def check_snoozed_tasks(bot: Bot) -> None:
    """Re-notify about tasks when snooze expires."""
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(
                Task.is_done.is_(False),
                Task.snoozed_until.is_not(None),
                Task.snoozed_until <= func.now(),
            )
        )
        tasks = list(result.scalars().all())

        for task in tasks:
            try:
                await bot.send_message(
                    chat_id=settings.telegram_owner_id,
                    text=f"⏰ <b>Нагадування:</b> {task.title}",
                    parse_mode="HTML",
                    reply_markup=task_keyboard(task.id),
                )
            except Exception:
                logger.exception(f"Failed to send snooze reminder for task {task.id}")
                continue
            task.snoozed_until = None

        if tasks:
            await session.commit()
            logger.info(f"Sent {len(tasks)} snooze reminders")


async def cleanup_old_data(bot: Bot) -> None:
    """Delete processed messages and done tasks older than 30 days."""
    cutoff = datetime.now(tz=ZoneInfo("UTC")) - timedelta(days=30)
    async with async_session() as session:
        msg_result = await session.execute(
            delete(Message).where(
                Message.status.in_(["processed", "notified"]),
                Message.created_at < cutoff,
            )
        )
        messages_deleted = msg_result.rowcount

        task_result = await session.execute(
            delete(Task).where(
                Task.is_done.is_(True),
                Task.done_at < cutoff,
            )
        )
        tasks_deleted = task_result.rowcount

        await session.commit()

    logger.info(f"Cleanup: deleted {messages_deleted} messages, {tasks_deleted} tasks")
