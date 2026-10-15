"""Application services: the single place for database access per domain.

Layering: browser -> panel/API routes -> service layer -> ORM -> PostgreSQL.
Routes handle HTTP, auth, and response mapping only; every query and state
change lives here so the panel and the machine API cannot drift apart, and
no route contains ad-hoc SQL. Services never audit with a UI identity (they
are told the acting user id) and never render.

This module also exposes the DB seam the HTTP layers use:
:func:`session_scope` (one session) and :func:`dispose_engine` (shutdown).
The panel imports ONLY from ``app.services`` (plus shared pure logic in
``app.messaging``/``app.news``); it never imports ``app.db`` or an ORM
model — enforced by ``tests/test_panel_layering.py``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.db.connection import async_session as _async_session
from app.db.connection import engine as _engine


@asynccontextmanager
async def session_scope():
    """Yield one database session (the only session entry point for routes)."""
    async with _async_session() as session:
        yield session


async def session_dep() -> AsyncIterator:
    """FastAPI dependency: one session per request.

    Provided here rather than in the panel so the HTTP layers never import
    ``app.db.connection`` themselves.
    """
    async with _async_session() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the shared engine pool (app shutdown)."""
    await _engine.dispose()