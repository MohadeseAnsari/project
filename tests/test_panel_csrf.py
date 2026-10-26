"""CSRF enforcement for the panel (A5): token + Origin + SameSite=Lax.

Uses a raw AsyncClient (no auto-token wrapper) so missing/invalid tokens are
exercised directly. Compliant-browser behavior (valid token) is covered by
the rest of the panel suite via ``tests/panel_test_client.py``.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.panel.app import panel_app
from app.panel.csrf import csrf_token_for_cookie
from app.services.passwords import hash_password


def _raw() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=panel_app), base_url="http://test")


async def _admin(session: AsyncSession, username: str = "csrf-admin") -> None:
    from sqlalchemy import select

    from app.db.models import PanelUser, Role

    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(
        PanelUser(username=username, password_hash=hash_password("secret123"),
                  role_id=role.id, is_active=True)
    )
    await session.commit()


async def _login(client: AsyncClient, username: str = "csrf-admin") -> None:
    response = await client.post(
        "/login", data={"username": username, "password": "secret123"},
        follow_redirects=False,
    )
    assert response.status_code == 302


@pytest.mark.asyncio
async def test_missing_token_rejected(session: AsyncSession):
    """POST without a token returns Persian 400, never a state change."""
    await _admin(session)
    async with _raw() as client:
        await _login(client)
        response = await client.post("/submit", data={
            "username": "t", "title": "T", "abstract": "A", "field": "f",
            "university": "U", "table_of_contents": "c",
            "author": "A", "supervisor": "S",
        }, follow_redirects=False)
        assert response.status_code == 400
        assert "توکن امنیتی نامعتبر" in response.text


@pytest.mark.asyncio
async def test_invalid_token_rejected(session: AsyncSession):
    """A forged token returns Persian 400."""
    await _admin(session)
    async with _raw() as client:
        await _login(client)
        response = await client.post(
            "/submit",
            data={
                "username": "t", "title": "T", "abstract": "A", "field": "f",
                "university": "U", "table_of_contents": "c",
                "author": "A", "supervisor": "S",
                "csrf_token": "forged-token",
            },
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "توکن امنیتی نامعتبر" in response.text


@pytest.mark.asyncio
async def test_valid_token_accepted(session: AsyncSession):
    """The session-bound token lets a compliant browser through."""
    from sqlalchemy import select

    from app.db.models import Main

    await _admin(session)
    async with _raw() as client:
        await _login(client)
        token = csrf_token_for_cookie(client.cookies.get("panel_session", ""))
        response = await client.post(
            "/submit",
            data={
                "username": "t", "title": "CSRF Valid", "abstract": "A", "field": "f",
                "university": "U", "table_of_contents": "c",
                "author": "A", "supervisor": "S",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
    row = (await session.execute(select(Main).where(Main.title == "CSRF Valid"))).scalar_one()
    assert row.status == "ready"


@pytest.mark.asyncio
async def test_foreign_origin_rejected(session: AsyncSession):
    """A cross-site Origin is rejected even with a valid token."""
    await _admin(session)
    async with _raw() as client:
        await _login(client)
        token = csrf_token_for_cookie(client.cookies.get("panel_session", ""))
        response = await client.post(
            "/submit",
            data={
                "username": "t", "title": "T", "abstract": "A", "field": "f",
                "university": "U", "table_of_contents": "c",
                "author": "A", "supervisor": "S",
                "csrf_token": token,
            },
            headers={"Origin": "https://evil.example", "Host": "test"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "مبدأ ناشناخته" in response.text


@pytest.mark.asyncio
async def test_login_exempt_from_token_but_not_origin(session: AsyncSession):
    """Login needs no token (pre-session) but still enforces Origin."""
    await _admin(session)
    async with _raw() as client:
        # No token, no Origin: allowed (302 or 401, never 400).
        response = await client.post(
            "/login", data={"username": "csrf-admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
    async with _raw() as client:
        response = await client.post(
            "/login", data={"username": "csrf-admin", "password": "secret123"},
            headers={"Origin": "https://evil.example", "Host": "test"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "مبدأ ناشناخته" in response.text


@pytest.mark.asyncio
async def test_forms_carry_token(session: AsyncSession):
    """Every rendered POST form embeds the hidden CSRF field."""
    await _admin(session)
    async with _raw() as client:
        await _login(client)
        for path in ("/submit", "/models", "/channels", "/users"):
            page = await client.get(path)
            assert page.status_code == 200, path
            assert 'name="csrf_token"' in page.text, path
