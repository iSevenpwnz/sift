import asyncio
import logging
from contextlib import asynccontextmanager

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from src.app.api.health import router as health_router
from src.app.bot.dispatcher import create_bot, create_dispatcher
from src.app.collectors.telegram import create_userbot, register_handlers
from src.app.config import settings
from src.app.db.session import engine
from src.app.processors.pipeline import message_processor, requeue_pending
from src.app.scheduler.jobs import daily_digest, retry_pending_ai

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logging.basicConfig(level=getattr(logging, settings.log_level.upper()), format="%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Sift starting...")

    # Message queue
    queue: asyncio.Queue = asyncio.Queue()

    # Telethon userbot
    userbot = create_userbot()
    register_handlers(userbot, queue)
    await userbot.connect()
    if not await userbot.is_user_authorized():
        logger.error("Telethon session not authorized. Run scripts/generate_session.py locally first.")
        raise RuntimeError("Telethon not authorized")
    logger.info("Telethon connected")

    # aiogram bot
    bot = create_bot()
    dp = create_dispatcher()
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    logger.info("aiogram bot started")

    # Re-queue pending messages from DB
    pending_ids = await requeue_pending()
    if pending_ids:
        logger.info(f"Re-queuing {len(pending_ids)} pending messages")
        for msg_id in pending_ids:
            await queue.put({"_requeue_id": msg_id})

    # Message processor
    processor_task = asyncio.create_task(message_processor(queue))

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_digest,
        CronTrigger(hour=settings.digest_hour, minute=0, timezone=settings.timezone),
        args=[bot],
        id="daily_digest",
    )
    scheduler.add_job(
        retry_pending_ai,
        IntervalTrigger(minutes=5),
        id="retry_pending_ai",
    )
    scheduler.start()
    logger.info("Scheduler started")

    logger.info("Sift ready")
    yield

    # Shutdown
    logger.info("Sift shutting down...")
    scheduler.shutdown(wait=True)
    processor_task.cancel()
    polling_task.cancel()
    await userbot.disconnect()
    await bot.session.close()
    await engine.dispose()
    logger.info("Sift stopped")


app = FastAPI(title="Sift", lifespan=lifespan)
app.include_router(health_router)
