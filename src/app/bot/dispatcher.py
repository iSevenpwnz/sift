from aiogram import Bot, Dispatcher

from src.app.bot.handlers.callbacks import router as callbacks_router
from src.app.bot.handlers.commands import router as commands_router
from src.app.bot.handlers.settings import router as settings_router
from src.app.bot.middleware import OwnerOnlyMiddleware
from src.app.config import settings


def create_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnlyMiddleware())
    dp.include_router(commands_router)
    dp.include_router(settings_router)
    dp.include_router(callbacks_router)
    return dp
