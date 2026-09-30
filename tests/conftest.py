import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.connection import engine
from app.db.models import Base


@pytest_asyncio.fixture
async def session():
    """Isolated in-memory SQLite session for unit tests.

    Provides the full schema (via ``Base.metadata.create_all``) without
    touching PostgreSQL, so Worker/classification tests stay fast and
    deterministic.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _dispose_shared_engine_pool():
    """Dispose the shared PostgreSQL engine's pool after each test.

    pytest-asyncio runs every test on a fresh event loop. Asyncpg
    connections left in the pool from a previous (closed) loop would fail the
    next test with "attached to a different loop". Disposing the pool after
    each test forces fresh connections on the next test's loop.
    """
    yield
    await engine.dispose()
