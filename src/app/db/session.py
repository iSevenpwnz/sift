from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.app.config import settings

engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=5)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
