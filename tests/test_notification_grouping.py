from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.processors.pipeline import (
    NotificationItem,
    _format_grouped,
    _format_single,
    _flush_notifications,
)


def _make_item(chat="Test Chat", sender="Igor", topic="Обговорення", category="meeting",
               priority="medium", task=None, time_str="14:12", content="some text"):
    msg = MagicMock()
    msg.source_chat = chat
    msg.sender = sender
    msg.content = content
    msg.id = 1
    msg.created_at = datetime(2026, 4, 1, 14, 12)
    ai_result = {"category": category, "priority": priority, "topic": topic}
    return NotificationItem(
        msg=msg, ai_result=ai_result, task=task,
        icon="📅", topic=topic, sender=sender,
        time_str=time_str, priority=priority,
    )


class TestFormatSingle:
    def test_basic(self):
        item = _make_item()
        text = _format_single(item)
        assert "<b>Обговорення</b>" in text
        assert "Test Chat" in text
        assert "Igor" in text

    def test_long_preview_truncated(self):
        item = _make_item(content="x" * 400)
        text = _format_single(item)
        assert "..." in text

    def test_with_date(self):
        item = _make_item()
        item.ai_result["date"] = "2026-04-02T15:00:00"
        text = _format_single(item)
        assert "📆" in text

    def test_with_people(self):
        item = _make_item()
        item.ai_result["people"] = ["Igor", "Mykhailo"]
        text = _format_single(item)
        assert "Igor, Mykhailo" in text


class TestFormatGrouped:
    def test_two_messages(self):
        items = [
            _make_item(sender="Igor", topic="Підвищення зарплати", time_str="14:12"),
            _make_item(sender="Mykhailo", topic="Демонстрація агента", time_str="14:19"),
        ]
        text = _format_grouped("The Code of Dragons", items)
        assert "The Code of Dragons" in text
        assert "2 повідомлення" in text
        assert "Igor • 14:12" in text
        assert "Mykhailo • 14:19" in text

    def test_five_messages_noun(self):
        items = [_make_item() for _ in range(5)]
        text = _format_grouped("Chat", items)
        assert "5 повідомлень" in text

    def test_three_messages_noun(self):
        items = [_make_item() for _ in range(3)]
        text = _format_grouped("Chat", items)
        assert "3 повідомлення" in text


class TestFlushNotifications:
    @pytest.mark.asyncio
    async def test_single_message_uses_single_format(self):
        bot = AsyncMock()
        item = _make_item(chat="Chat A")
        with patch("src.app.processors.pipeline.settings") as mock_settings:
            mock_settings.telegram_owner_id = 123
            await _flush_notifications(bot, [item])

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert "<b>Обговорення</b>" in call_kwargs["text"]
        assert "Chat A" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_grouped_messages_from_same_chat(self):
        bot = AsyncMock()
        items = [
            _make_item(chat="Chat A", sender="Igor", topic="Topic 1", time_str="14:12"),
            _make_item(chat="Chat A", sender="Mykhailo", topic="Topic 2", time_str="14:19"),
        ]
        with patch("src.app.processors.pipeline.settings") as mock_settings:
            mock_settings.telegram_owner_id = 123
            await _flush_notifications(bot, items)

        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs["text"]
        assert "2 повідомлення" in text
        assert "Igor • 14:12" in text
        assert "Mykhailo • 14:19" in text

    @pytest.mark.asyncio
    async def test_different_chats_send_separate(self):
        bot = AsyncMock()
        items = [
            _make_item(chat="Chat A", topic="Topic A"),
            _make_item(chat="Chat B", topic="Topic B"),
        ]
        with patch("src.app.processors.pipeline.settings") as mock_settings:
            mock_settings.telegram_owner_id = 123
            await _flush_notifications(bot, items)

        assert bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_high_priority_enables_notification(self):
        bot = AsyncMock()
        items = [
            _make_item(chat="Chat A", priority="low"),
            _make_item(chat="Chat A", priority="high"),
        ]
        with patch("src.app.processors.pipeline.settings") as mock_settings:
            mock_settings.telegram_owner_id = 123
            await _flush_notifications(bot, items)

        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["disable_notification"] is False

    @pytest.mark.asyncio
    async def test_task_keyboard_on_grouped(self):
        task = MagicMock()
        task.id = 42
        items = [
            _make_item(chat="Chat A", topic="Do something", task=task),
            _make_item(chat="Chat A", topic="Another thing"),
        ]
        with patch("src.app.processors.pipeline.settings") as mock_settings:
            mock_settings.telegram_owner_id = 123
            await _flush_notifications(bot=AsyncMock(), buffer=items)
