from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from src.app.db.models import Task, UserSettings
from src.app.db.session import async_session
from src.app.config import settings

router = Router()


# ── Chat approval (from Telethon collector) ─────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("approve:"))
async def approve_chat(callback: CallbackQuery) -> None:
    chat_id = callback.data.split(":", 1)[1]
    await _set_chat_decision(chat_id, "monitored")

    # Clear pending cache so collector starts processing
    from src.app.collectors.telegram import _pending_approval, _chat_decisions
    import src.app.collectors.telegram as tg_mod
    _pending_approval.discard(chat_id)
    _chat_decisions[chat_id] = "monitored"
    tg_mod._chat_decisions_loaded_at = 0

    await callback.answer("🔔 Моніторимо!")
    if callback.message:
        original_text = callback.message.text or ""
        await callback.message.edit_text(f"✅ {original_text}\n\n<b>→ Моніторимо</b>", parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("reject:"))
async def reject_chat(callback: CallbackQuery) -> None:
    chat_id = callback.data.split(":", 1)[1]
    await _set_chat_decision(chat_id, "ignored")

    from src.app.collectors.telegram import _pending_approval, _chat_decisions
    import src.app.collectors.telegram as tg_mod
    _pending_approval.discard(chat_id)
    _chat_decisions[chat_id] = "ignored"
    tg_mod._chat_decisions_loaded_at = 0

    await callback.answer("🔇 Ігноруємо!")
    if callback.message:
        original_text = callback.message.text or ""
        await callback.message.edit_text(f"🔇 {original_text}\n\n<b>→ Ігноруємо</b>", parse_mode="HTML")


async def _set_chat_decision(chat_id: str, decision: str) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == settings.telegram_owner_id)
        )
        us = result.scalar_one_or_none()
        if not us:
            us = UserSettings(telegram_user_id=settings.telegram_owner_id)
            session.add(us)
            await session.flush()

        monitored = list(us.monitored_chats or [])
        ignored = list(us.ignored_chats or [])

        # Remove from both lists first
        monitored = [c for c in monitored if str(c) != chat_id]
        ignored = [c for c in ignored if str(c) != chat_id]

        if decision == "monitored":
            monitored.append(chat_id)
        else:
            ignored.append(chat_id)

        us.monitored_chats = monitored
        us.ignored_chats = ignored
        await session.commit()


@router.callback_query(lambda c: c.data and c.data.startswith("task_done:"))
async def task_done(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task:
            task.is_done = True
            task.done_at = datetime.now(timezone.utc)
            await session.commit()
            await callback.answer("✅ Готово!")
            if callback.message:
                await callback.message.edit_text(
                    f"✅ <s>{task.title}</s> — виконано", parse_mode="HTML", reply_markup=None
                )
        else:
            await callback.answer("Таск не знайдено")


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
            until = task.snoozed_until.strftime("%H:%M")
            await callback.answer(f"⏰ Нагадаю о {until}")
            if callback.message:
                await callback.message.edit_text(
                    f"⏰ <b>{task.title}</b>\n<i>Відкладено до {until}</i>",
                    parse_mode="HTML",
                    reply_markup=None,
                )
        else:
            await callback.answer("Task not found")


# ── Digest navigation ───────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("digest:"))
async def digest_navigate(callback: CallbackQuery) -> None:
    from datetime import date as date_type
    from src.app.scheduler.jobs import build_digest, _send_digest

    date_str = callback.data.split(":", 1)[1]
    try:
        target = date_type.fromisoformat(date_str)
    except ValueError:
        await callback.answer("Невірна дата")
        return

    # Answer immediately to prevent timeout
    await callback.answer("⏳")

    try:
        await callback.message.edit_text("⏳ Генерую дайджест...")
    except Exception:
        pass

    content, keyboard = await build_digest(target)

    try:
        if isinstance(content, list):
            await callback.message.delete()
            await _send_digest(callback.bot, content, keyboard)
        else:
            await callback.message.edit_text(text=content, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        pass
