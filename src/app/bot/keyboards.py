from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Done", callback_data=f"task_done:{task_id}"),
            InlineKeyboardButton(text="Snooze 1h", callback_data=f"task_snooze:{task_id}:1"),
            InlineKeyboardButton(text="Snooze 1d", callback_data=f"task_snooze:{task_id}:24"),
        ]
    ])
