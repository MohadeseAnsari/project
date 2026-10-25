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
from tests.panel_test_client import panel_client


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


def _panel():
    """CSRF-compliant panel client (browser simulation; see panel_test_client)."""
    return panel_client()


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
        assert created.status_code == 303
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


def test_client_for_channel_prefers_row_token(monkeypatch):
    """Per-channel variable wins; missing variable fails loudly; else platform client."""
    from app.messaging.platforms import client_for_channel

    class _Row:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _Stub:
        platform = "bale"

    platform_stub = _Stub()
    monkeypatch.setenv("CHAN_TEST_TOKEN", "chan-secret")
    client, error = client_for_channel(
        _Row(id=1, platform="bale", bot_token_env="CHAN_TEST_TOKEN"),
        {"bale": platform_stub},
    )
    assert error is None
    assert client is not platform_stub
    assert client._token == "chan-secret"

    client, error = client_for_channel(
        _Row(id=2, platform="bale", bot_token_env="CHAN_MISSING_VAR"), {"bale": platform_stub}
    )
    assert client is None
    assert "CHAN_MISSING_VAR" in error
    assert "chan-secret" not in error

    client, error = client_for_channel(
        _Row(id=3, platform="bale", bot_token_env=None), {"bale": platform_stub}
    )
    assert (client, error) == (platform_stub, None)

    client, error = client_for_channel(_Row(id=4, platform="bale", bot_token_env=None), {})
    assert client is None
    assert "no bot token" in error


@pytest.mark.asyncio
async def test_delivery_uses_channel_token(session: AsyncSession, monkeypatch):
    """Sender sends through the channel's own bot when configured."""
    from sqlalchemy import select as _select

    from app.db.models import Category, Delivery, Field, Main
    from app.messaging.dispatcher import claim_next_delivery, deliver_one

    monkeypatch.setenv("CHAN_SEND_TOKEN", "chan-secret")
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()
    field = Field(name="tok-field", normalized_name="tok-field")
    session.add(field)
    await session.commit()
    channel = Channel(platform="bale", chat_id=73001, username="@c", bot_token_env="CHAN_SEND_TOKEN")
    session.add(channel)
    await session.commit()
    article = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review", field_id=field.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="pending"))
    await session.commit()

    used = {}

    class _Recording:
        platform = "bale"

        async def send_text(self, chat_id: int, text: str) -> int:
            used["chat_id"] = chat_id
            return 4242

    import app.messaging.platforms as platforms

    monkeypatch.setattr(platforms, "build_client", lambda platform, token: _Recording() if token == "chan-secret" else (_ for _ in ()).throw(AssertionError("wrong token")))
    claimed = await claim_next_delivery(session)
    assert await deliver_one(session, claimed, {}) is True
    assert used["chat_id"] == 73001
