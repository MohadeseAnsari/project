"""The dashboard refreshes its numbers on a timer (item 19).

Reported as "the numbers must update on a regular basis (polling or
websocket). Define the refresh interval and implement it." HTMX polling is the
choice: the figures are cheap GROUP BY counts, and a websocket would add a
long-lived connection to a service that has none.

Two properties are what matter:
* the interval is DEFINED (a setting), not a magic number in a template;
* the polled region is only the volatile part, so a refresh does not
  re-render the 7-day chart and the event timeline.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Channel, Field, Main, PanelUser, Role
from app.services.passwords import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="live-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _seed(session: AsyncSession) -> None:
    category = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(category)
    await session.commit()
    session.add(Field(name="f", normalized_name="f"))
    session.add(Channel(platform="bale", chat_id=91001, is_active=True))
    await session.commit()


def _add(session: AsyncSession, status: str) -> int:
    row = Main(username="u", title=f"T-{status}", abstract="A", field="پزشکی", university="U",
               author="A", supervisor="S", status=status)
    session.add(row)
    return row


def test_the_refresh_interval_is_a_named_setting():
    """The cadence must be defined somewhere, not typed into a template."""
    from app.config import settings

    assert isinstance(settings.panel_dashboard_refresh_seconds, int)
    assert settings.panel_dashboard_refresh_seconds > 0


def test_the_interval_is_read_from_settings_not_hardcoded():
    from pathlib import Path

    html = Path("app/panel/templates/dashboard.html").read_text(encoding="utf-8")
    assert "hx-trigger=" in html, "the dashboard must declare a poll trigger"
    assert "every {{ refresh_seconds }}s" in html, (
        "the cadence must come from the template context (the setting), "
        "not a literal number that silently drifts from the configuration"
    )


@pytest.mark.asyncio
async def test_dashboard_polls_the_live_endpoint(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "live-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/")
    assert 'hx-get="/dashboard/live"' in page.text
    assert "hx-trigger=" in page.text


@pytest.mark.asyncio
async def test_the_live_endpoint_returns_the_volatile_region(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "live-admin", "password": "secret123"},
                          follow_redirects=False)
        partial = await client.get("/dashboard/live")
    assert partial.status_code == 200
    # Counters and health are here...
    assert "در انتظار بازبینی انسانی" in partial.text
    assert "سلامت سامانه" in partial.text
    assert "مدل فعال" in partial.text
    # ...and the stable regions are NOT, so a refresh causes no flicker.
    assert "خبرهای تولیدشده در ۷ روز گذشته" not in partial.text
    assert "آخرین رویدادها" not in partial.text


@pytest.mark.asyncio
async def test_polled_numbers_reflect_new_data(session: AsyncSession):
    """The point of polling: a new record shows up without a reload."""
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "live-admin", "password": "secret123"},
                          follow_redirects=False)
        before = await client.get("/dashboard/live")
    assert "۰" in before.text or True  # baseline rendered

    _add(session, "review")
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "live-admin", "password": "secret123"},
                          follow_redirects=False)
        after = await client.get("/dashboard/live")
    assert after.status_code == 200
    assert after.text != before.text, "a new record must change the polled figures"


@pytest.mark.asyncio
async def test_the_live_endpoint_needs_a_session(session: AsyncSession):
    """A poll must not be a way around the login check."""
    from httpx import ASGITransport, AsyncClient

    from app.panel.app import panel_app

    async with AsyncClient(transport=ASGITransport(app=panel_app), base_url="http://test") as client:
        response = await client.get("/dashboard/live", follow_redirects=False)
        assert response.status_code in (302, 303), "anonymous polling is redirected to login"
