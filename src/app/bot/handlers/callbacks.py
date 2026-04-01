from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.types import CallbackQuery

from src.app.db.models import Task
from src.app.db.session import async_session

router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("task_done:"))
async def task_done(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task:
            task.is_done = True
            task.done_at = datetime.now(timezone.utc)
            await session.commit()
            await callback.answer("Done!")
            if callback.message:
                await callback.message.edit_text(f"~{task.title}~ — done", parse_mode="MarkdownV2")
        else:
            await callback.answer("Task not found")


@router.callback_query(lambda c: c.data and c.data.startswith("task_snooze:"))
async def task_snooze(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[1])
    hours = int(parts[2]) if len(parts) > 2 else 1

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task:
            task.snoozed_until = datetime.now(timezone.utc) + timedelta(hours=hours)
            await session.commit()
            await callback.answer(f"Snoozed for {hours}h")
        else:
            await callback.answer("Task not found")
