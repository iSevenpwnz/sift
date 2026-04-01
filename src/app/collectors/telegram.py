import asyncio
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from src.app.config import settings
from src.app.processors.pipeline import persist_raw

logger = logging.getLogger(__name__)

_entity_cache: dict[int, str] = {}


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
