import asyncio
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from sqlalchemy import select

from src.app.config import settings
from src.app.db.models import UserSettings
from src.app.db.session import async_session
from src.app.processors.pipeline import persist_raw

logger = logging.getLogger(__name__)

_entity_cache: dict[int, str] = {}
_ignored_chats: set[str] = set()
_ignored_chats_loaded_at: float = 0


async def _load_ignored_chats() -> set[str]:
    """Load ignored chats from DB. Cache for 60 seconds."""
    import time

    global _ignored_chats, _ignored_chats_loaded_at
    now = time.monotonic()
    if now - _ignored_chats_loaded_at < 60:
        return _ignored_chats

    async with async_session() as session:
        result = await session.execute(
            select(UserSettings.ignored_chats).where(
                UserSettings.telegram_user_id == settings.telegram_owner_id
            )
        )
        row = result.scalar_one_or_none()
        _ignored_chats = set(str(c) for c in (row or []))
        _ignored_chats_loaded_at = now
    return _ignored_chats


def create_userbot() -> TelegramClient:
    return TelegramClient(
        StringSession(settings.telethon_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )


def register_handlers(client: TelegramClient, queue: asyncio.Queue) -> None:
    @client.on(events.NewMessage)
    async def on_new_message(event: events.NewMessage.Event) -> None:
        try:
            if event.out and event.is_private:
                return

            chat_id = event.chat_id

            # Check ignored chats
            ignored = await _load_ignored_chats()
            if str(chat_id) in ignored:
                return

            chat_title = _entity_cache.get(chat_id)
            if chat_title is None:
                chat = await event.get_chat()
                chat_title = getattr(chat, "title", getattr(chat, "first_name", "Unknown"))
                _entity_cache[chat_id] = chat_title

            reply_text = None
            if event.reply_to_msg_id:
                try:
                    reply_msg = await event.get_reply_message()
                    reply_text = reply_msg.text if reply_msg else None
                except Exception:
                    pass

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

            # Persist to DB immediately (write-ahead)
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

            chat_id = event.chat_id
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
