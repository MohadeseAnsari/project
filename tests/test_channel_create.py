"""Tests for getChat resolution and username-based channel creation."""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel
from app.messaging.platforms import (
    PermanentDeliveryError,
    TransientDeliveryError,
    build_client,
)
from app.panel.app import panel_app
from app.panel.auth import hash_password


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient; records GET params for getChat."""

    response = FakeResponse(200, {"ok": True, "result": {"id": 12345, "title": "Test Channel", "username": "testchannel"}})
    raises: Exception | None = None
    last_get: tuple | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        raise AssertionError("getChat tests must not POST")

    async def get(self, url, params=None):
        FakeAsyncClient.last_get = (url, params)
        if FakeAsyncClient.raises:
            raise FakeAsyncClient.raises
        return FakeAsyncClient.response


@pytest.fixture
def stub_get(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.raises = None
    FakeAsyncClient.last_get = None
    FakeAsyncClient.response = FakeResponse(
        200, {"ok": True, "result": {"id": 12345, "title": "Test Channel", "username": "testchannel"}}
    )
    return FakeAsyncClient


@pytest.mark.asyncio
async def test_get_chat_returns_result(stub_get):
    """getChat resolves an @username to id/title through the provider."""
    client = build_client("bale", "token")
    result = await client.get_chat("@testchannel")
    assert result["id"] == 12345
    assert result["title"] == "Test Channel"
    url, params = stub_get.last_get
    assert url.endswith("/bottoken/getChat")
    assert params == {"chat_id": "@testchannel"}


@pytest.mark.asyncio
async def test_get_chat_unknown_is_permanent(stub_get):
    """Unknown chats fail fast (panel shows 422, never saves)."""
    stub_get.response = FakeResponse(400, text="chat not found")
    with pytest.raises(PermanentDeliveryError):
        await build_client("telegram", "token").get_chat("@nope")


@pytest.mark.asyncio
async def test_get_chat_timeout_is_transient(stub_get):
    stub_get.raises = httpx.ConnectTimeout("timed out")
    with pytest.raises(TransientDeliveryError):
        await build_client("bale", "token").get_chat(12345)


async def _admin(session: AsyncSession) -> None:
    from sqlalchemy import select as _select

    from app.db.models import PanelUser, Role

    role = (await session.execute(_select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(
        PanelUser(username="chan-admin", password_hash=hash_password("secret123"),
                  role_id=role.id, is_active=True)
    )
    await session.commit()


def _panel() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=panel_app), base_url="http://test")


@pytest.mark.asyncio
async def test_channel_create_by_username_resolves_and_saves(session: AsyncSession, stub_get, monkeypatch):
    """@username creation stores the resolved numeric id; title is shown after."""
    from app.config import settings

    monkeypatch.setattr(settings, "bale_bot_token", "token")
    await _admin(session)
    async with _panel() as client:
        login = await client.post(
            "/login", data={"username": "chan-admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert login.status_code == 302
        created = await client.post(
            "/channels", data={"platform": "bale", "target": "@testchannel", "title": ""},
            follow_redirects=False,
        )
        assert created.status_code == 302
    row = (await session.execute(select(Channel).where(Channel.chat_id == 12345))).scalar_one()
    assert row.platform == "bale"
    assert row.username == "testchannel"
    assert row.title == "Test Channel"
    async with _panel() as client:
        await client.post("/login", data={"username": "chan-admin", "password": "secret123"})
        page = await client.get("/channels")
        assert "Test Channel" in page.text


@pytest.mark.asyncio
async def test_channel_create_unknown_fails_without_row(session: AsyncSession, stub_get, monkeypatch):
    """Unresolvable targets return 422 and create nothing."""
    from app.config import settings

    monkeypatch.setattr(settings, "bale_bot_token", "token")
    stub_get.response = FakeResponse(400, text="chat not found")
    await _admin(session)
    async with _panel() as client:
        await client.post("/login", data={"username": "chan-admin", "password": "secret123"})
        response = await client.post(
            "/channels", data={"platform": "bale", "target": "@nope", "title": ""}
        )
        assert response.status_code == 422
    assert (await session.execute(select(Channel))).scalars().all() == []
