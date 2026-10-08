"""Test configuration: PostgreSQL-only test database.

The application and all tests use PostgreSQL exclusively (no SQLite). A
dedicated ``irandoc_news_test`` database is created from the configured
connection, the real SQL migrations are applied to it, and each test runs
against a truncated copy of the schema.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Point the application at the PostgreSQL test database BEFORE any app
# import, so settings.database_url (and the shared engine) use it.
_MAIN_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/irandoc_news"
)
_PARTS = urlsplit(_MAIN_URL)
TEST_DB_NAME = "irandoc_news_test"
# SQLAlchemy engine URL (keeps the +asyncpg driver suffix).
TEST_DB_URL = urlunsplit((_PARTS.scheme, _PARTS.netloc, f"/{TEST_DB_NAME}", _PARTS.query, _PARTS.fragment))
os.environ["DATABASE_URL"] = TEST_DB_URL
# asyncpg.connect() URL (no driver suffix).
_PG_SCHEME = "postgresql" if _PARTS.scheme.startswith("postgresql") else _PARTS.scheme
_PG_URL = urlunsplit((_PG_SCHEME, _PARTS.netloc, f"/{TEST_DB_NAME}", _PARTS.query, _PARTS.fragment))

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.db.connection import async_session, engine


def _admin_url(db_name: str) -> str:
    return urlunsplit((_PG_SCHEME, _PARTS.netloc, f"/{db_name}", _PARTS.query, _PARTS.fragment))


def _pg_url(db_name: str) -> str:
    return urlunsplit((_PG_SCHEME, _PARTS.netloc, f"/{db_name}", _PARTS.query, _PARTS.fragment))


@pytest_asyncio.fixture(scope="session")
async def test_database():
    """Create the PostgreSQL test database and apply the real migrations.

    Drops any previous test database for a clean slate, recreates it, and
    applies every SQL migration in order. This is the authoritative schema
    for the tests — no ORM create_all, no SQLite.
    """
    admin = await asyncpg.connect(_admin_url("postgres"))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            TEST_DB_NAME,
        )
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        await admin.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    finally:
        await admin.close()

    conn = await asyncpg.connect(_pg_url(TEST_DB_NAME))
    try:
        for migration_file in sorted(Path("migrations").glob("*.sql")):
            await conn.execute(migration_file.read_text(encoding="utf-8"))
    finally:
        await conn.close()

    yield

    admin = await asyncpg.connect(_admin_url("postgres"))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            TEST_DB_NAME,
        )
        await admin.execute(f"DROP DATABASE {TEST_DB_NAME}")
    finally:
        await admin.close()


@pytest_asyncio.fixture
async def session(test_database):
    """A PostgreSQL session with a clean schema for each test.

    Truncates all tables so each test starts from an empty, known state and
    inserts its own data. Uses the shared application engine (pointing at the
    test database) so tests exercise the real production schema.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE deliveries, field_channel_routes, category_channel_routes, channels, "
                "main, fields, categories, models, prompts, "
                "audit_logs, panel_users RESTART IDENTITY CASCADE"
            )
        )
    async with async_session() as s:
        yield s


@pytest_asyncio.fixture(autouse=True)
async def _dispose_shared_engine_pool():
    """Dispose the shared engine's pool after each test.

    pytest-asyncio runs every test on a fresh event loop; asyncpg
    connections left in the pool from a closed loop would fail the next test.
    """
    yield
    await engine.dispose()
