import time

from fastapi import APIRouter
from sqlalchemy import func, select

from src.app.db.models import Message
from src.app.db.session import async_session

router = APIRouter()

_start_time = time.monotonic()


@router.get("/health")
async def health() -> dict:
    async with async_session() as session:
        result = await session.execute(
            select(func.max(Message.created_at))
        )
        last_message = result.scalar_one_or_none()

        result = await session.execute(
            select(func.count()).where(Message.status.in_(["raw", "pending_ai"]))
        )
        pending = result.scalar_one()

    return {
        "status": "ok",
        "last_message_at": last_message.isoformat() if last_message else None,
        "pending_messages": pending,
        "uptime_seconds": int(time.monotonic() - _start_time),
    }
