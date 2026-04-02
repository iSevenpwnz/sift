import asyncio
import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from src.app.config import settings
from src.app.db.models import UserSettings
from src.app.db.session import async_session
from src.app.processors.pipeline import persist_raw

logger = logging.getLogger(__name__)

_entity_cache: dict[int, str] = {}

# Chat approval state: "monitored" | "ignored" | None (unknown)
_chat_decisions: dict[str, str] = {}
_chat_decisions_loaded_at: float = 0
_pending_approval: set[str] = set()  # chats we already asked about


async def _load_chat_decisions() -> dict[str, str]:
    """Load monitored/ignored chats from DB. Cache for 60s."""
    global _chat_decisions, _chat_decisions_loaded_at
    now = time.monotonic()
    if now - _chat_decisions_loaded_at < 60:
        return _chat_decisions

    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == settings.telegram_owner_id)
        )
        us = result.scalar_one_or_none()

    decisions = {}
    if us:
        for chat_id in (us.monitored_chats or []):
            decisions[str(chat_id)] = "monitored"
        for chat_id in (us.ignored_chats or []):
            decisions[str(chat_id)] = "ignored"

    _chat_decisions = decisions
    _chat_decisions_loaded_at = now
    return decisions


def _approval_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Моніторити", callback_data=f"approve:{chat_id}", style="success"),
        InlineKeyboardButton(text="🔇 Ігнорувати", callback_data=f"reject:{chat_id}", style="danger"),
    ]])


def create_userbot() -> TelegramClient:
    return TelegramClient(
        StringSession(settings.telethon_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )


def register_handlers(client: TelegramClient, queue: asyncio.Queue, bot: Bot | None = None) -> None:
    @client.on(events.NewMessage)
    async def on_new_message(event: events.NewMessage.Event) -> None:
        try:
            # Skip Saved Messages
            if event.out and event.is_private:
                return

            chat_id = event.chat_id
            chat_id_str = str(chat_id)
            is_private = event.is_private  # DM

            # Skip messages from the bot itself
            bot_id = int(settings.telegram_bot_token.split(":")[0])
            if chat_id == bot_id:
                return

            # DM — always process
            if not is_private:
                decisions = await _load_chat_decisions()
                decision = decisions.get(chat_id_str)

                if decision == "ignored":
                    return

                if decision is None:
                    # Unknown chat — ask owner
                    if chat_id_str not in _pending_approval and bot:
                        _pending_approval.add(chat_id_str)
                        chat = await event.get_chat()
                        chat_title = getattr(chat, "title", getattr(chat, "first_name", "Unknown"))
                        _entity_cache[chat_id] = chat_title

                        try:
                            await bot.send_message(
                                chat_id=settings.telegram_owner_id,
                                text=f"🆕 <b>Новий чат виявлено:</b>\n<i>{chat_title}</i>",
                                parse_mode="HTML",
                                reply_markup=_approval_keyboard(chat_id_str),
                                disable_notification=True,
                            )
                        except Exception:
                            logger.exception("Failed to send approval request")
                    return  # Don't process until approved

                # decision == "monitored" — continue processing

            # Resolve chat title
            chat_title = _entity_cache.get(chat_id)
            if chat_title is None:
                chat = await event.get_chat()
                chat_title = getattr(chat, "title", getattr(chat, "first_name", "Unknown"))
                _entity_cache[chat_id] = chat_title

            # Reply context
            reply_text = None
            if event.reply_to_msg_id:
                try:
                    reply_msg = await event.get_reply_message()
                    reply_text = reply_msg.text if reply_msg else None
                except Exception:
                    pass

            # Content type
            content_type = "text"
            content = event.text or ""

            if event.photo:
                content_type = "photo"
                content = event.message.message or ""
            elif event.document:
                content_type = "document"
                content = event.message.message or ""
            elif event.sticker:
                return
            elif event.gif:
                return

            if not content.strip():
                return

            sender = event.sender
            sender_name = getattr(sender, "first_name", "") or ""
            if hasattr(sender, "last_name") and sender.last_name:
                sender_name = f"{sender_name} {sender.last_name}"

            msg_data = {
                "source": "telegram",
                "source_id": f"tg_{event.id}_{chat_id}",
                "source_chat": chat_title,
                "sender": sender_name.strip() or "Unknown",
                "content": content,
                "content_type": content_type,
                "reply_to_text": reply_text,
                "raw_metadata": {"chat_id": chat_id, "message_id": event.id},
            }

            msg_id = await persist_raw(msg_data)
            if msg_id:
                await queue.put(msg_id)

        except Exception:
            logger.exception("Error handling Telegram message")

    @client.on(events.MessageEdited)
    async def on_message_edited(event: events.MessageEdited.Event) -> None:
        try:
            if event.out and event.is_private:
                return

            # Skip bot's own edits
            bot_id = int(settings.telegram_bot_token.split(":")[0])
            if event.chat_id == bot_id:
                return

            chat_id = event.chat_id
            chat_id_str = str(chat_id)

            if not event.is_private:
                decisions = await _load_chat_decisions()
                if decisions.get(chat_id_str) != "monitored":
                    return

            chat_title = _entity_cache.get(chat_id, "Unknown")
            content = event.text or ""
            if not content.strip():
                return

            msg_data = {
                "source": "telegram",
                "source_id": f"tg_{event.id}_{chat_id}",
                "source_chat": chat_title,
                "sender": "",
                "content": content,
                "content_type": "text",
                "reply_to_text": None,
                "raw_metadata": {"chat_id": chat_id, "message_id": event.id, "edited": True},
            }

            msg_id = await persist_raw(msg_data)
            if msg_id:
                await queue.put(msg_id)

        except Exception:
            logger.exception("Error handling edited message")
