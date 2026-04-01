from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from src.app.db.models import Message as DbMessage, Task
from src.app.db.session import async_session

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Sift is running.\n\n"
        "Commands:\n"
        "/summary — today's digest\n"
        "/tasks — active tasks\n"
    )


@router.message(Command("summary"))
async def cmd_summary(message: Message) -> None:
    async with async_session() as session:
        # Count today's messages by category
        today = func.current_date()
        result = await session.execute(
            select(DbMessage.category, func.count())
            .where(func.date(DbMessage.created_at) == today)
            .where(DbMessage.status != "raw")
            .group_by(DbMessage.category)
        )
        stats = dict(result.all())

    total = sum(stats.values())
    if not total:
        await message.answer("Nothing processed today yet.")
        return

    lines = [f"Today: {total} messages processed\n"]
    for cat in ["meeting", "task", "deadline", "info", "noise"]:
        count = stats.get(cat, 0)
        if count:
            lines.append(f"  {cat}: {count}")

    await message.answer("\n".join(lines))


@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.is_done.is_(False))
            .order_by(Task.due_date.asc().nulls_last())
            .limit(20)
        )
        tasks = list(result.scalars().all())

    if not tasks:
        await message.answer("No active tasks.")
        return

    lines = ["Active tasks:\n"]
    for i, task in enumerate(tasks, 1):
        due = f" (due: {task.due_date.strftime('%d.%m')})" if task.due_date else ""
        lines.append(f"{i}. {task.title}{due}")

    await message.answer("\n".join(lines))
