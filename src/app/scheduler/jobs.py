import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select

from src.app.constants import CATEGORY_ICONS
from src.app.db.models import ChatDailySummary, Message, Task, UserSettings
from src.app.db.session import async_session
from src.app.config import settings
from src.app.bot.keyboards import task_keyboard
from src.app.processors.pipeline import process_messages
from src.app.processors.ai_provider import get_primary_provider, get_fallback_provider

logger = logging.getLogger(__name__)

USER_TZ = ZoneInfo("Europe/Kyiv")

def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _msg_link(msg) -> str | None:
    """Build t.me link to original message. Returns URL or None."""
    meta = msg.raw_metadata or {}
    chat_id = meta.get("chat_id")
    message_id = meta.get("message_id")
    if not chat_id or not message_id:
        return None
    # Private channels: -100XXXXXXXXXX → t.me/c/XXXXXXXXXX/msg_id
    cid = str(chat_id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    # DMs — no public link
    return None


async def _summarize_groups(groups: dict[str, list[Message]]) -> dict[str, str]:
    """Ask AI to summarize each chat group. Top 10 chats only, truncated content."""
    # Sort by message count, take top 10
    sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]))[:10]

    items = []
    for chat_name, msgs in sorted_groups:
        messages_text = []
        for m in msgs[:8]:  # max 8 messages per chat
            sender = m.sender or ""
            text = m.content[:150]  # shorter to fit free tier
            if sender and sender != "Unknown":
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
        response = await asyncio.wait_for(
            provider.client.chat.completions.create(
                model=provider.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
            ),
            timeout=20,
        )
        text = response.choices[0].message.content or "{}"
        parsed = json.loads(text)
        return parsed.get("summaries", {})
    except Exception:
        logger.warning("Failed to generate digest summaries, using fallback")
        return {}  # empty = _render_chat will build its own summary from topics


def _digest_nav_keyboard(target_date: date) -> InlineKeyboardMarkup:
    """Navigation buttons for digest: ◀️ Prev day / Next day ▶️"""
    prev_date = target_date - timedelta(days=1)
    next_date = target_date + timedelta(days=1)
    today = datetime.now(USER_TZ).date()

    buttons = [InlineKeyboardButton(text=f"◀️ {prev_date.strftime('%d.%m')}", callback_data=f"digest:{prev_date.isoformat()}")]
    if target_date < today:
        buttons.append(InlineKeyboardButton(text="Сьогодні", callback_data=f"digest:{today.isoformat()}"))
        buttons.append(InlineKeyboardButton(text=f"{next_date.strftime('%d.%m')} ▶️", callback_data=f"digest:{next_date.isoformat()}"))

    return InlineKeyboardMarkup(inline_keyboard=[buttons])


_digest_cache: dict[str, tuple] = {}  # date_iso → (content, keyboard, timestamp)


async def build_digest(target_date: date) -> tuple[str | list[str], InlineKeyboardMarkup]:
    """Build digest. Cached for 5 minutes."""
    import html as html_mod
    import time

    cache_key = target_date.isoformat()
    cached = _digest_cache.get(cache_key)
    if cached and time.monotonic() - cached[2] < 300:  # 5 min cache
        return cached[0], cached[1]

    async with async_session() as session:
        # Load ignored chats
        us_result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == settings.telegram_owner_id)
        )
        us = us_result.scalar_one_or_none()
        ignored_ids = set(str(c) for c in (us.ignored_chats if us else []))

        result = await session.execute(
            select(Message)
            .where(func.date(Message.created_at) == target_date)
            .where(Message.category.is_not(None))
            .where(Message.category != "noise")
            .where(Message.source_chat.is_not(None))
            .where(Message.source_chat != "Unknown")
            .order_by(Message.created_at.desc())
        )
        # Filter out ignored chats
        all_msgs = [
            m for m in result.scalars().all()
            if str((m.raw_metadata or {}).get("chat_id", "")) not in ignored_ids
        ]

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

        result = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .where((Task.snoozed_until.is_(None)) | (Task.snoozed_until <= func.now()))
            .order_by(Task.due_date.asc().nulls_last())
            .limit(10)
        )
        tasks = list(result.scalars().all())

        result = await session.execute(
            select(Task.message_id).where(Task.is_done.is_(True)).where(Task.message_id.is_not(None))
        )
        done_msg_ids = set(result.scalars().all())

    if not all_msgs and not tasks:
        date_str = target_date.strftime("%d.%m")
        result = (f"📊 Дайджест за {date_str}\n\nНічого важливого.", _digest_nav_keyboard(target_date))
        _digest_cache[cache_key] = (result[0], result[1], time.monotonic())
        return result

    e = html_mod.escape  # shorthand

    # Group by chat
    chat_groups: dict[str, list[Message]] = defaultdict(list)
    for msg in all_msgs:
        chat_groups[msg.source_chat or "Інше"].append(msg)

    # Classify: DM / group / channel
    dm_chats = {}
    group_chats = {}
    channel_chats = {}
    for chat_name, msgs in chat_groups.items():
        meta = msgs[0].raw_metadata or {}
        chat_id = meta.get("chat_id", 0)
        if isinstance(chat_id, int) and chat_id > 0:
            dm_chats[chat_name] = msgs
        elif all(not m.sender or m.sender == "Unknown" for m in msgs[:5]):
            channel_chats[chat_name] = msgs
        else:
            group_chats[chat_name] = msgs

    # Load pre-built summaries from DB, fallback to AI generation
    async with async_session() as session:
        summary_result = await session.execute(
            select(ChatDailySummary).where(ChatDailySummary.summary_date == target_date)
        )
        db_summaries = {s.chat_name: s.summary_text for s in summary_result.scalars().all()}
    summaries = db_summaries
    if not summaries:
        summaries = await _summarize_groups(chat_groups)

    # ── Build HTML ──
    date_str = target_date.strftime("%d.%m")
    useful = total - noise
    lines = [f"<b>📊 Дайджест за {date_str}</b>  •  {useful} корисних, {noise} шум\n"]

    def _noun(count: int) -> str:
        return "повідомлення" if 2 <= count <= 4 else "повідомлень" if count >= 5 else "повідомлення"

    def _short_name(name: str, max_len: int = 35) -> str:
        """Truncate long chat names."""
        if len(name) <= max_len:
            return name
        return name[:max_len - 1] + "…"

    def _render_chat(chat_name: str, msgs: list):
        count = len(msgs)
        summary = summaries.get(chat_name, "")
        short = _short_name(chat_name)

        # Collect deduped action items (skip done tasks)
        actions = []
        seen = set()
        for msg in msgs:
            if msg.category in ("meeting", "deadline", "task"):
                if msg.id in done_msg_ids:
                    continue
                topic = msg.extracted_topic or msg.content[:60]
                key = topic[:25].lower()
                if key in seen:
                    continue
                seen.add(key)
                ic = CATEGORY_ICONS.get(msg.category, "📌")
                date_info = ""
                if msg.extracted_date:
                    date_info = f" — {msg.extracted_date.strftime('%d.%m %H:%M')}"
                actions.append(f"{ic} {e(topic)}{date_info}")
        actions = actions[:3]

        lines.append(f"\n💬 <b>{e(short)}</b> — {count} {_noun(count)}")

        # Everything in one expandable blockquote
        block_parts = []
        if summary:
            block_parts.append(e(summary))
        else:
            # No AI summary — build from topics with links
            seen_t = set()
            topic_lines = []
            for msg in msgs[:8]:
                t = msg.extracted_topic or msg.content[:50]
                key = t[:20].lower()
                if key in seen_t:
                    continue
                seen_t.add(key)
                link = _msg_link(msg)
                if link:
                    topic_lines.append(f'• <a href="{link}">{e(t)}</a>')
                else:
                    topic_lines.append(f"• {e(t)}")
            if topic_lines:
                block_parts.append("\n".join(topic_lines))

        if actions:
            block_parts.append("\n".join(actions))

        if block_parts:
            block_text = "\n\n".join(block_parts)
            lines.append(f"<blockquote expandable>{block_text}</blockquote>")

    # DMs — always show all
    if dm_chats:
        lines.append(f"\n<b>👤 Особисті</b>")
        for cn, ms in sorted(dm_chats.items(), key=lambda x: -len(x[1])):
            _render_chat(cn, ms)

    # Groups — top 5, rest collapsed
    if group_chats:
        lines.append(f"\n<b>💬 Групи</b>")
        sorted_groups = sorted(group_chats.items(), key=lambda x: -len(x[1]))
        for cn, ms in sorted_groups[:5]:
            _render_chat(cn, ms)
        if len(sorted_groups) > 5:
            rest_names = [_short_name(cn, 25) for cn, _ in sorted_groups[5:]]
            lines.append(f"\n<blockquote expandable>Також: {', '.join(e(n) for n in rest_names)}</blockquote>")

    # Channels — top 5 with >1 message, singles grouped
    if channel_chats:
        lines.append(f"\n<b>📰 Канали</b>")
        sorted_channels = sorted(channel_chats.items(), key=lambda x: -len(x[1]))
        main_channels = [(cn, ms) for cn, ms in sorted_channels if len(ms) > 1]
        single_channels = [(cn, ms) for cn, ms in sorted_channels if len(ms) == 1]

        for cn, ms in main_channels[:5]:
            _render_chat(cn, ms)

        # Singles — compact list in one expandable block, with links
        if single_channels:
            parts = []
            for cn, ms in single_channels:
                topic = e(ms[0].extracted_topic or ms[0].content[:50])
                link = _msg_link(ms[0])
                name = e(_short_name(cn, 30))
                if link:
                    parts.append(f'• {name}: <a href="{link}">{topic}</a>')
                else:
                    parts.append(f"• {name}: {topic}")
            lines.append(f"\n<blockquote expandable>{chr(10).join(parts)}</blockquote>")

    # Tasks
    lines.append("")
    if tasks:
        lines.append(f"<b>📋 Активні таски ({len(tasks)}):</b>")
        for task in tasks:
            due = f" • до {task.due_date.strftime('%d.%m %H:%M')}" if task.due_date else ""
            lines.append(f"  • {e(task.title)}{due}")
    else:
        lines.append("📋 Активних тасків: 0")

    text = "\n".join(lines)

    # If too long — split into 2 messages
    if len(text) > 4000:
        # Find a good split point (after Групи section)
        split_marker = "\n<b>📰 Канали</b>"
        if split_marker in text:
            part1 = text[:text.index(split_marker)]
            part2 = text[text.index(split_marker):]
            result = ([part1, part2], _digest_nav_keyboard(target_date))
            _digest_cache[cache_key] = (result[0], result[1], time.monotonic())
            return result
        text = text[:3990] + "\n\n<i>...обрізано</i>"

    kb = _digest_nav_keyboard(target_date)
    _digest_cache[cache_key] = (text, kb, time.monotonic())
    return text, kb


async def _send_digest(bot: Bot, content, keyboard: InlineKeyboardMarkup) -> None:
    """Send digest — handles string, list of strings. No link previews."""
    from aiogram.types import LinkPreviewOptions
    no_preview = LinkPreviewOptions(is_disabled=True)

    if isinstance(content, list):
        for i, part in enumerate(content):
            is_last = i == len(content) - 1
            await bot.send_message(
                chat_id=settings.telegram_owner_id,
                text=part,
                parse_mode="HTML",
                link_preview_options=no_preview,
                reply_markup=keyboard if is_last else None,
            )
    else:
        await bot.send_message(
            chat_id=settings.telegram_owner_id,
            text=content,
            parse_mode="HTML",
            link_preview_options=no_preview,
            reply_markup=keyboard,
        )


async def daily_digest(bot: Bot) -> None:
    """Send daily digest to owner."""
    today = datetime.now(USER_TZ).date()
    content, keyboard = await build_digest(today)
    await _send_digest(bot, content, keyboard)


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


async def check_reminders(bot: Bot) -> None:
    """Send reminders when their time comes."""
    from src.app.db.models import Reminder
    import html as html_mod

    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(
                Reminder.sent.is_(False),
                Reminder.remind_at <= func.now(),
            )
        )
        reminders = list(result.scalars().all())

        for rem in reminders:
            try:
                # Load the original message for context
                msg = await session.get(Message, rem.message_id) if rem.message_id else None
                topic = ""
                if msg:
                    topic = msg.extracted_topic or msg.content[:100]

                text = f"🔔 <b>Нагадування</b>\n{html_mod.escape(topic)}"
                if msg and msg.source_chat:
                    text += f"\n<i>{html_mod.escape(msg.source_chat)}</i>"

                await bot.send_message(
                    chat_id=settings.telegram_owner_id,
                    text=text,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception(f"Failed to send reminder {rem.id}")
                continue
            rem.sent = True

        if reminders:
            await session.commit()
            logger.info(f"Sent {len(reminders)} reminders")


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

        summary_result = await session.execute(
            delete(ChatDailySummary).where(ChatDailySummary.summary_date < cutoff.date())
        )
        summaries_deleted = summary_result.rowcount

        await session.commit()

    logger.info(f"Cleanup: deleted {messages_deleted} messages, {tasks_deleted} tasks, {summaries_deleted} summaries")
