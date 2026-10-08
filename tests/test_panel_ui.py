"""Smoke tests for the server-rendered review panel (templates + one HTMX part)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.panel.app import panel_app
from app.panel.auth import hash_password


async def _user(session: AsyncSession, username: str = "panel-tester") -> None:
    """Create an admin user directly (login is exercised over HTTP below)."""
    from sqlalchemy import select

    from app.db.models import PanelUser, Role

    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(
        PanelUser(
            username=username,
            password_hash=hash_password("secret123"),
            role_id=role.id,
            is_active=True,
        )
    )
    await session.commit()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=panel_app), base_url="http://test")


@pytest.mark.asyncio
async def test_login_template_is_persian_rtl_with_local_htmx():
    """Login page renders Persian RTL with locally vendored HTMX (no CDN)."""
    async with _client() as client:
        response = await client.get("/login")
    assert response.status_code == 200
    html = response.text
    assert 'dir="rtl"' in html
    assert "ورود به پنل بررسی" in html
    assert "/static/htmx.min.js" in html
    assert "unpkg.com" not in html
    assert "/static/style.css" in html


@pytest.mark.asyncio
async def test_dashboard_requires_login(session: AsyncSession):
    """Anonymous browsers are sent to /login instead of seeing data."""
    async with _client() as client:
        response = await client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_and_htmx_records_table(session: AsyncSession):
    """Full login over HTTP, then the HTMX records partial returns a table."""
    await _user(session)
    async with _client() as client:
        login = await client.post(
            "/login", data={"username": "panel-tester", "password": "secret123"},
            follow_redirects=False,
        )
        assert login.status_code == 302
        assert "panel_session" in login.cookies
        dashboard = await client.get("/")
        assert dashboard.status_code == 200
        assert "داشبورد" in dashboard.text
        # The one HTMX endpoint test: partial table HTML, not JSON.
        table = await client.get("/records/table", params={"page": 1, "page_size": 5})
        assert table.status_code == 200
        assert "<table>" in table.text
        assert "مجموع" in table.text
