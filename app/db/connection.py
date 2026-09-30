"""Async SQLAlchemy engine and session factory (single shared engine)."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a scoped async session.

    The session is closed when the request finishes; callers commit
    explicitly. Used by the API routes and directly by the Worker loop.
    """
    async with async_session() as session:
        yield session
