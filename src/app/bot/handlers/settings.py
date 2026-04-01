"""
/settings — interactive settings menu.

Architecture: one message, edited in-place. FSM tracks which screen is active.
Screens: main → chats (with pagination) → quiet hours → timezone → digest time
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from src.app.config import settings as app_settings
from src.app.db.models import UserSettings
from src.app.db.session import async_session

router = Router()

CHATS_PER_PAGE = 6
TIMEZONES = ["Europe/Kyiv", "Europe/Warsaw", "Europe/London", "America/New_York", "Asia/Tokyo", "UTC"]


class Settings(StatesGroup):
    main = State()
    chats = State()
    quiet_hours = State()
    timezone = State()
    digest = State()


# ── DB helpers ──────────────────────────────────────────────

async def get_or_create_settings(user_id: int) -> UserSettings:
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.telegram_user_id == user_id)
        )
        us = result.scalar_one_or_none()
        if not us:
            us = UserSettings(telegram_user_id=user_id)
            session.add(us)
            await session.commit()
            await session.refresh(us)
        return us


async def get_known_chats(user_id: int) -> list[dict]:
    """Get unique chats from messages DB — real chats the user has."""
    from src.app.db.models import Message

    async with async_session() as session:
        result = await session.execute(
            select(Message.raw_metadata["chat_id"].as_string(), Message.source_chat)
            .where(Message.source == "telegram")
            .where(Message.source_chat.is_not(None))
            .group_by(Message.raw_metadata["chat_id"].as_string(), Message.source_chat)
            .order_by(Message.source_chat)
        )
        rows = result.all()

    chats = []
    seen = set()
    for chat_id_str, chat_name in rows:
        if chat_id_str not in seen and chat_name:
            seen.add(chat_id_str)
            chats.append({"id": chat_id_str, "name": chat_name})
    return chats


# ── Keyboard builders ───────────────────────────────────────

def main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Чати для моніторингу", callback_data="nav:chats")
    builder.button(text="🔕 Тихі години", callback_data="nav:quiet")
    builder.button(text="🌍 Часовий пояс", callback_data="nav:tz")
    builder.button(text="📊 Час дайджесту", callback_data="nav:digest")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Закрити", callback_data="nav:close"))
    return builder.as_markup()


def chats_keyboard(
    chats: list[dict], ignored: list[str], page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    start = page * CHATS_PER_PAGE
    page_chats = chats[start : start + CHATS_PER_PAGE]

    for chat in page_chats:
        is_ignored = chat["id"] in ignored
        icon = "🔇" if is_ignored else "🔔"
        name = chat["name"][:30]
        builder.button(text=f"{icon} {name}", callback_data=f"t:{chat['id']}")
    builder.adjust(1)

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"pg:{page - 1}"))
    total_pages = max(1, (len(chats) + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE)
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if start + CHATS_PER_PAGE < len(chats):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"pg:{page + 1}"))
    builder.row(*nav)

    builder.row(InlineKeyboardButton(text="🔇 Вимкнути всі канали", callback_data="t:mute_channels"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="nav:main"))
    return builder.as_markup()


def quiet_keyboard(quiet_hours: dict) -> InlineKeyboardMarkup:
    start = quiet_hours.get("start", "—")
    end = quiet_hours.get("end", "—")
    enabled = bool(start != "—")

    builder = InlineKeyboardBuilder()
    if enabled:
        builder.button(text=f"🌙 {start} — {end}", callback_data="noop")
        builder.button(text="❌ Вимкнути тихі години", callback_data="qh:off")
    else:
        builder.button(text="Тихі години вимкнені", callback_data="noop")

    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="22:00–08:00", callback_data="qh:22:08"),
        InlineKeyboardButton(text="23:00–07:00", callback_data="qh:23:07"),
    )
    builder.row(
        InlineKeyboardButton(text="00:00–09:00", callback_data="qh:00:09"),
        InlineKeyboardButton(text="21:00–06:00", callback_data="qh:21:06"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="nav:main"))
    return builder.as_markup()


def timezone_keyboard(current_tz: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for tz in TIMEZONES:
        mark = " ✅" if tz == current_tz else ""
        builder.button(text=f"{tz}{mark}", callback_data=f"tz:{tz}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="nav:main"))
    return builder.as_markup()


def digest_keyboard(current_hour: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for hour in ["07:00", "08:00", "09:00", "10:00", "12:00", "18:00"]:
        mark = " ✅" if hour == current_hour else ""
        builder.button(text=f"{hour}{mark}", callback_data=f"dg:{hour}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="nav:main"))
    return builder.as_markup()


# ── Entry point ─────────────────────────────────────────────

@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    us = await get_or_create_settings(message.from_user.id)
    await state.set_state(Settings.main)
    await message.answer(
        "⚙️ <b>Налаштування Sift</b>\n\n"
        f"🌍 Часовий пояс: {us.timezone}\n"
        f"📊 Дайджест: {us.digest_time}\n"
        f"🔕 Тихі години: {us.quiet_hours.get('start', 'вимк.')}–{us.quiet_hours.get('end', '')}\n"
        f"🔇 Ігноровані чати: {len(us.ignored_chats)}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ── Navigation ──────────────────────────────────────────────

@router.callback_query(F.data == "nav:main")
async def nav_main(callback: CallbackQuery, state: FSMContext) -> None:
    us = await get_or_create_settings(callback.from_user.id)
    await state.set_state(Settings.main)
    await callback.message.edit_text(
        "⚙️ <b>Налаштування Sift</b>\n\n"
        f"🌍 Часовий пояс: {us.timezone}\n"
        f"📊 Дайджест: {us.digest_time}\n"
        f"🔕 Тихі години: {us.quiet_hours.get('start', 'вимк.')}–{us.quiet_hours.get('end', '')}\n"
        f"🔇 Ігноровані чати: {len(us.ignored_chats)}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "nav:close")
async def nav_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("⚙️ Налаштування закрито.")
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ── Chats screen ────────────────────────────────────────────

@router.callback_query(F.data == "nav:chats")
async def nav_chats(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Settings.chats)
    await state.update_data(page=0)

    us = await get_or_create_settings(callback.from_user.id)
    chats = await get_known_chats(callback.from_user.id)
    ignored = [str(c) for c in (us.ignored_chats or [])]

    active = len(chats) - len([c for c in chats if c["id"] in ignored])

    await callback.message.edit_text(
        f"📝 <b>Чати для моніторингу</b>\n\n"
        f"🔔 Активні: {active}  |  🔇 Ігноровані: {len(ignored)}\n"
        f"Натисни щоб увімкнути/вимкнути:",
        parse_mode="HTML",
        reply_markup=chats_keyboard(chats, ignored, page=0),
    )
    await callback.answer()


@router.callback_query(Settings.chats, F.data.startswith("t:"))
async def toggle_chat(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = callback.data.split(":", 1)[1]

    us = await get_or_create_settings(callback.from_user.id)
    ignored = list(us.ignored_chats or [])

    if chat_id == "mute_channels":
        # Mute all channels (negative IDs starting with -100)
        chats = await get_known_chats(callback.from_user.id)
        for chat in chats:
            cid = chat["id"]
            if cid.startswith("-100") and cid not in ignored:
                ignored.append(cid)
    elif chat_id in ignored:
        ignored.remove(chat_id)
    else:
        ignored.append(chat_id)

    async with async_session() as session:
        db_us = await session.get(UserSettings, us.id)
        db_us.ignored_chats = ignored
        await session.commit()

    data = await state.get_data()
    page = data.get("page", 0)
    chats = await get_known_chats(callback.from_user.id)

    active = len(chats) - len([c for c in chats if c["id"] in ignored])

    try:
        await callback.message.edit_text(
            f"📝 <b>Чати для моніторингу</b>\n\n"
            f"🔔 Активні: {active}  |  🔇 Ігноровані: {len(ignored)}\n"
            f"Натисни щоб увімкнути/вимкнути:",
            parse_mode="HTML",
            reply_markup=chats_keyboard(chats, [str(c) for c in ignored], page=page),
        )
    except Exception:
        pass  # MessageNotModified
    await callback.answer("Оновлено!")


@router.callback_query(Settings.chats, F.data.startswith("pg:"))
async def paginate_chats(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    await state.update_data(page=page)

    us = await get_or_create_settings(callback.from_user.id)
    chats = await get_known_chats(callback.from_user.id)
    ignored = [str(c) for c in (us.ignored_chats or [])]

    await callback.message.edit_reply_markup(
        reply_markup=chats_keyboard(chats, ignored, page=page)
    )
    await callback.answer()


# ── Quiet hours ─────────────────────────────────────────────

@router.callback_query(F.data == "nav:quiet")
async def nav_quiet(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Settings.quiet_hours)
    us = await get_or_create_settings(callback.from_user.id)
    await callback.message.edit_text(
        "🔕 <b>Тихі години</b>\n\n"
        "В цей час бот не надсилатиме нотифікації.\n"
        "Обери варіант або вимкни:",
        parse_mode="HTML",
        reply_markup=quiet_keyboard(us.quiet_hours or {}),
    )
    await callback.answer()


@router.callback_query(Settings.quiet_hours, F.data.startswith("qh:"))
async def set_quiet_hours(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data[3:]  # "22:08" or "off"

    us = await get_or_create_settings(callback.from_user.id)
    if value == "off":
        quiet = {}
    else:
        start, end = value.split(":")
        quiet = {"start": f"{start}:00", "end": f"{end}:00"}

    async with async_session() as session:
        db_us = await session.get(UserSettings, us.id)
        db_us.quiet_hours = quiet
        await session.commit()

    await callback.message.edit_reply_markup(reply_markup=quiet_keyboard(quiet))
    await callback.answer("Збережено!" if quiet else "Тихі години вимкнено!")


# ── Timezone ────────────────────────────────────────────────

@router.callback_query(F.data == "nav:tz")
async def nav_timezone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Settings.timezone)
    us = await get_or_create_settings(callback.from_user.id)
    await callback.message.edit_text(
        f"🌍 <b>Часовий пояс</b>\n\nПоточний: {us.timezone}",
        parse_mode="HTML",
        reply_markup=timezone_keyboard(us.timezone),
    )
    await callback.answer()


@router.callback_query(Settings.timezone, F.data.startswith("tz:"))
async def set_timezone(callback: CallbackQuery, state: FSMContext) -> None:
    tz = callback.data[3:]
    us = await get_or_create_settings(callback.from_user.id)

    async with async_session() as session:
        db_us = await session.get(UserSettings, us.id)
        db_us.timezone = tz
        await session.commit()

    await callback.message.edit_text(
        f"🌍 <b>Часовий пояс</b>\n\nПоточний: {tz}",
        parse_mode="HTML",
        reply_markup=timezone_keyboard(tz),
    )
    await callback.answer(f"Встановлено: {tz}")


# ── Digest time ─────────────────────────────────────────────

@router.callback_query(F.data == "nav:digest")
async def nav_digest(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Settings.digest)
    us = await get_or_create_settings(callback.from_user.id)
    await callback.message.edit_text(
        f"📊 <b>Час дайджесту</b>\n\nЗараз: {us.digest_time}\n"
        "Щоденний дайджест надсилається о цій годині:",
        parse_mode="HTML",
        reply_markup=digest_keyboard(us.digest_time),
    )
    await callback.answer()


@router.callback_query(Settings.digest, F.data.startswith("dg:"))
async def set_digest(callback: CallbackQuery, state: FSMContext) -> None:
    time_str = callback.data[3:]
    us = await get_or_create_settings(callback.from_user.id)

    async with async_session() as session:
        db_us = await session.get(UserSettings, us.id)
        db_us.digest_time = time_str
        await session.commit()

    await callback.message.edit_text(
        f"📊 <b>Час дайджесту</b>\n\nЗараз: {time_str}\n"
        "Щоденний дайджест надсилається о цій годині:",
        parse_mode="HTML",
        reply_markup=digest_keyboard(time_str),
    )
    await callback.answer(f"Дайджест о {time_str}")
