from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder


def main_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom navigation — always visible."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="📊 Дайджест")
    builder.button(text="📋 Таски")
    builder.button(text="📅 Тиждень")
    builder.button(text="⚙️ Налаштування")
    builder.adjust(2, 2)
    return builder.as_markup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію...",
    )


def task_keyboard(task_id: int, msg_id: int | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text="✅ Готово", callback_data=f"task_done:{task_id}", style="success"),
        InlineKeyboardButton(text="⏰ 1г", callback_data=f"task_snooze:{task_id}:1", style="primary"),
        InlineKeyboardButton(text="📆 1д", callback_data=f"task_snooze:{task_id}:24", style="primary"),
    ]
    rows = [buttons]
    if msg_id:
        rows.append([InlineKeyboardButton(text="↩️ Відповісти", callback_data=f"reply:{msg_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notification_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    """Keyboard for non-task notifications — just reply button."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Відповісти", callback_data=f"reply:{msg_id}")]
    ])
