"""Channels are a LIST: add, edit, switch on/off, delete — independently (B).

The owner reported that adding a channel in the panel "replaced" the one they
already had. These tests pin the behaviour that fixes it: creating a channel
always INSERTs a second row, editing one channel never touches another, the
switch works per channel, and delete removes a channel that never published
while a channel with history is only disabled.

Creating from the panel also has to work without an environment change, so a
bot token can be typed into the form; it is stored encrypted and never echoed
back.
"""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    Category,
    CategoryChannelRoute,
    Channel,
    Delivery,
    Field,
    FieldChannelRoute,
    Main,
    PanelUser,
    Role,
)
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


class _Result:
    """A getChat answer, so channel creation can be exercised offline."""

    def __init__(self, chat_id: int, title: str, username: str):
        self.chat_id = chat_id
        self.title = title
        self.username = username


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeGetChat:
    """Stands in for httpx.AsyncClient and answers getChat with a fixed result."""

    result = _Result(555001, "Resolved Channel", "resolved")
    status = 200
    last_get: tuple | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        raise AssertionError("channel lifecycle tests must not POST")

    async def get(self, url, params=None):
        FakeGetChat.last_get = (url, params)
        if FakeGetChat.status != 200:
            return FakeResponse(FakeGetChat.status, text="chat not found")
        return FakeResponse(
            200,
            {"ok": True, "result": {"id": self.result.chat_id, "title": self.result.title,
                                    "username": self.result.username}},
        )


@pytest.fixture(autouse=True)
def _platform_token(monkeypatch):
    """A platform-wide bot token, so creation works without a per-channel one.

    The one test about "no token anywhere" turns this off explicitly.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "bale_bot_token", "PLATFORM-TOKEN")
    monkeypatch.setattr(settings, "telegram_bot_token", "")


@pytest.fixture
def stub_get_chat(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeGetChat)
    FakeGetChat.status = 200
    FakeGetChat.result = _Result(555001, "Resolved Channel", "resolved")
    FakeGetChat.last_get = None
    return FakeGetChat


async def _admin(session: AsyncSession, username: str = "chan-owner") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _login(client, username: str = "chan-owner"):
    response = await client.post(
        "/login", data={"username": username, "password": "secret123"}, follow_redirects=False
    )
    assert response.status_code == 302


async def _add_via_panel(client, target: str, title: str = "", token: str = "") -> None:
    response = await client.post(
        "/channels",
        data={"platform": "bale", "target": target, "title": title, "bot_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


async def _channels(session: AsyncSession) -> list[Channel]:
    return list((await session.execute(select(Channel).order_by(Channel.id))).scalars().all())


# --------------------------------------------------------------------------
# add: a new channel joins the existing ones
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adding_a_channel_keeps_the_existing_one(session: AsyncSession, stub_get_chat):
    """THE reported bug: adding must not replace, it must add a second row."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(1001, "First Channel", "first")
        await _add_via_panel(client, "@first", title="اولی")
        stub_get_chat.result = _Result(1002, "Second Channel", "second")
        await _add_via_panel(client, "@second", title="دومی")
    rows = await _channels(session)
    assert [r.chat_id for r in rows] == [1001, 1002]
    assert [r.title for r in rows] == ["اولی", "دومی"]
    assert all(r.is_active for r in rows), "a new channel starts enabled"


@pytest.mark.asyncio
async def test_adding_a_channel_does_not_repoint_the_sender(session: AsyncSession, stub_get_chat):
    """Both channels keep their own destination: no shared row is reused."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(2001, "A", "a")
        await _add_via_panel(client, "@a")
        stub_get_chat.result = _Result(2002, "B", "b")
        await _add_via_panel(client, "@b")
    rows = {r.title: r.chat_id for r in await _channels(session)}
    assert rows == {"A": 2001, "B": 2002}


@pytest.mark.asyncio
async def test_channels_page_lists_every_channel_with_its_own_controls(
    session: AsyncSession, stub_get_chat
):
    """Both rows are visible at once, each with its own edit link and switch."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(3001, "Alpha", "alpha")
        await _add_via_panel(client, "@alpha")
        stub_get_chat.result = _Result(3002, "Beta", "beta")
        await _add_via_panel(client, "@beta")
        page = await client.get("/channels")
    rows = await _channels(session)
    assert "Alpha" in page.text and "Beta" in page.text
    for row in rows:
        assert f"/channels?channel_id={row.id}" in page.text, "each channel is selectable"
        assert f"/channels/{row.id}/toggle" in page.text, "each channel has its own switch"
    # The edit/delete controls live with the channel you are working on.
    async with panel_client() as client:
        await _login(client)
        for row in rows:
            detail = await client.get(f"/channels?channel_id={row.id}")
            assert f'"/channels/{row.id}"' in detail.text, "its own edit form"
            assert f"/channels/{row.id}/delete" in detail.text, "its own delete action"


# --------------------------------------------------------------------------
# edit: one channel, never the others
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_changes_only_the_targeted_channel(session: AsyncSession, stub_get_chat):
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(4001, "Keep Me", "keepme")
        await _add_via_panel(client, "@keepme", title="دست‌نخورده")
        stub_get_chat.result = _Result(4002, "Edit Me", "editme")
        await _add_via_panel(client, "@editme", title="قبل از ویرایش")
        first, second = await _channels(session)
        response = await client.post(
            f"/channels/{second.id}",
            data={"title": "بعد از ویرایش", "chat_id": str(second.chat_id),
                  "username": "editme", "bot_token_env": ""},
            follow_redirects=False,
        )
        assert response.status_code == 302
    await session.refresh(first)
    await session.refresh(second)
    assert first.title == "دست‌نخورده", "editing channel B must not rewrite channel A"
    assert first.chat_id == 4001
    assert second.title == "بعد از ویرایش"


@pytest.mark.asyncio
async def test_edit_to_an_existing_destination_is_refused(session: AsyncSession, stub_get_chat):
    """Two channels cannot share one (platform, chat_id) — and neither is mutated."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(5001, "First", "first")
        await _add_via_panel(client, "@first")
        stub_get_chat.result = _Result(5002, "Second", "second")
        await _add_via_panel(client, "@second")
        first, second = await _channels(session)
        response = await client.post(
            f"/channels/{second.id}",
            data={"title": "Second", "chat_id": str(first.chat_id), "username": "second",
                  "bot_token_env": ""},
            follow_redirects=False,
        )
        assert response.status_code == 422
    await session.refresh(second)
    assert second.chat_id == 5002, "the refused edit left the row alone"


# --------------------------------------------------------------------------
# on / off
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_flips_one_channel_only(session: AsyncSession, stub_get_chat):
    """Turning a channel off stops it alone; the other keeps publishing."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(6001, "On", "on")
        await _add_via_panel(client, "@on")
        stub_get_chat.result = _Result(6002, "Other", "other")
        await _add_via_panel(client, "@other")
        first, second = await _channels(session)
        assert (await client.post(f"/channels/{first.id}/toggle", follow_redirects=False)).status_code == 303
        assert (await client.post(f"/channels/{first.id}/toggle", follow_redirects=False)).status_code == 303
        page = await client.get("/channels")
    await session.refresh(first)
    await session.refresh(second)
    assert first.is_active is True, "two clicks return it to enabled"
    assert second.is_active is True, "the other channel is unaffected"
    assert "غیرفعال" in page.text or "فعال" in page.text


@pytest.mark.asyncio
async def test_disabled_channel_drops_out_of_routing(session: AsyncSession, stub_get_chat):
    """A switched-off channel is not a delivery target, even with a category route."""
    from app.messaging.dispatcher import channels_for_article

    await _admin(session)
    category = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(category)
    await session.commit()
    field = Field(name="off-field", normalized_name="off-field", category_id=category.id)
    session.add(field)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(7001, "Off", "off")
        await _add_via_panel(client, "@off")
        stub_get_chat.result = _Result(7002, "On", "on")
        await _add_via_panel(client, "@on")
    off, on = await _channels(session)
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=off.id))
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=on.id))
    article = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U", author="A",
        supervisor="S", status="approved", field_id=field.id, category_id=category.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        await client.post(f"/channels/{off.id}/toggle", follow_redirects=False)
    routed = await channels_for_article(session, article)
    assert [c.id for c in routed] == [on.id], "only the enabled channel is a target"


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


async def _route(session: AsyncSession, channel: Channel) -> None:
    category = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(category)
    await session.commit()
    field = Field(name="routed-field", normalized_name="routed-field", category_id=category.id)
    session.add(field)
    await session.commit()
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channel.id))
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=channel.id))
    await session.commit()


@pytest.mark.asyncio
async def test_delete_removes_an_unused_channel_and_its_routes(
    session: AsyncSession, stub_get_chat
):
    """A channel that never published is really removed, routes and all."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(8001, "Keep", "keep")
        await _add_via_panel(client, "@keep")
        stub_get_chat.result = _Result(8002, "Drop", "drop")
        await _add_via_panel(client, "@drop")
        keep, drop = await _channels(session)
        await _route(session, drop)
        response = await client.post(
            f"/channels/{drop.id}/delete", data={"confirm": "yes"}, follow_redirects=False
        )
        assert response.status_code == 303
    assert [r.id for r in await _channels(session)] == [keep.id], "the other channel survives"
    assert (await session.execute(
        select(FieldChannelRoute).where(FieldChannelRoute.channel_id == drop.id)
    )).scalars().all() == []
    assert (await session.execute(
        select(CategoryChannelRoute).where(CategoryChannelRoute.channel_id == drop.id)
    )).scalars().all() == []


@pytest.mark.asyncio
async def test_delete_of_a_published_channel_only_disables_it(session: AsyncSession, stub_get_chat):
    """History is never orphaned: a channel with deliveries is switched off."""
    await _admin(session)
    article = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U", author="A",
        supervisor="S", status="published", output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(9001, "Published", "published")
        await _add_via_panel(client, "@published")
    channel = (await _channels(session))[0]
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="sent",
                         platform_message_id=555))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            f"/channels/{channel.id}/delete", data={"confirm": "yes"}, follow_redirects=False
        )
        assert response.status_code == 303
    await session.refresh(channel)
    assert channel.is_active is False, "disabled, not deleted"
    detail = (await session.execute(
        select(AuditLog.detail).where(AuditLog.action == "channel_delete")
    )).scalars().all()
    assert detail and "outcome=disabled" in detail[0]


@pytest.mark.asyncio
async def test_delete_without_confirmation_does_nothing(session: AsyncSession, stub_get_chat):
    """A stray click cannot drop a destination: confirmation is required."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(9101, "Safe", "safe")
        await _add_via_panel(client, "@safe")
    channel = (await _channels(session))[0]
    async with panel_client() as client:
        await _login(client)
        response = await client.post(f"/channels/{channel.id}/delete", data={},
                                     follow_redirects=False)
        assert response.status_code == 303
    await session.refresh(channel)
    assert channel is not None
    assert (await session.execute(select(Channel).where(Channel.id == channel.id))).scalar_one() is not None
    assert channel.is_active is True


@pytest.mark.asyncio
async def test_viewer_can_add_no_channel(session: AsyncSession, stub_get_chat):
    """Channel management follows the same role rule as the rest of config."""
    role = (await session.execute(select(Role).where(Role.name == "viewer"))).scalar_one_or_none()
    if role is None:
        role = Role(name="viewer", description="viewer")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="chan-viewer", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()
    async with panel_client() as client:
        await _login(client, "chan-viewer")
        created = await client.post(
            "/channels", data={"platform": "bale", "target": "@nope"}, follow_redirects=False
        )
        assert created.status_code == 403
    assert await _channels(session) == []


# --------------------------------------------------------------------------
# per-channel bot token: adding works with no environment change
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_token_from_the_form_is_stored_encrypted_and_never_echoed(
    session: AsyncSession, stub_get_chat, monkeypatch
):
    """The token typed into the form is what validates the target, and stays secret."""
    from app.config import settings
    from app.credentials import decrypt
    from app.messaging.platforms import client_for_channel

    monkeypatch.setattr(settings, "bale_bot_token", "")
    secret = "12345678:SUPER-SECRET-TOKEN"
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(11001, "Token Channel", "tokchan")
        response = await client.post(
            "/channels",
            data={"platform": "bale", "target": "@tokchan", "title": "با توکن", "bot_token": secret},
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = await client.get("/channels")
    assert secret not in page.text, "the token must never be rendered back"
    channel = (await _channels(session))[0]
    assert channel.bot_token_encrypted and secret not in channel.bot_token_encrypted
    assert decrypt(channel.bot_token_encrypted) == secret
    _, error = client_for_channel(channel, {})
    assert error is None, "the stored token is usable with no platform-wide client"
    url, params = stub_get_chat.last_get
    assert secret in url, "the form token is what validated the target"


@pytest.mark.asyncio
async def test_blank_token_on_edit_keeps_the_stored_one(session: AsyncSession, stub_get_chat):
    """Saving the edit form with an empty token box does not unbreak the channel."""
    from app.credentials import decrypt

    secret = "99999988:KEEP-ME"
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(12001, "Keep Token", "keeptoken")
        await client.post(
            "/channels",
            data={"platform": "bale", "target": "@keeptoken", "title": "قبل", "bot_token": secret},
            follow_redirects=False,
        )
        channel = (await _channels(session))[0]
        response = await client.post(
            f"/channels/{channel.id}",
            data={"title": "بعد", "chat_id": str(channel.chat_id), "username": "keeptoken",
                  "bot_token_env": "", "bot_token": ""},
            follow_redirects=False,
        )
        assert response.status_code == 302
    await session.refresh(channel)
    assert channel.title == "بعد"
    assert decrypt(channel.bot_token_encrypted) == secret


@pytest.mark.asyncio
async def test_clear_token_is_explicit(session: AsyncSession, stub_get_chat):
    """Clearing is a separate checkbox, never a side effect of saving."""
    from app.credentials import decrypt

    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        stub_get_chat.result = _Result(13001, "Clearable", "clearable")
        await client.post(
            "/channels",
            data={"platform": "bale", "target": "@clearable", "title": "پاک‌شدنی",
                  "bot_token": "5555:TO-BE-CLEARED"},
            follow_redirects=False,
        )
        channel = (await _channels(session))[0]
        assert decrypt(channel.bot_token_encrypted)
        response = await client.post(
            f"/channels/{channel.id}",
            data={"title": "پاک‌شدنی", "chat_id": str(channel.chat_id), "username": "clearable",
                  "bot_token_env": "", "clear_token": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 302
    await session.refresh(channel)
    assert channel.bot_token_encrypted is None
    async with panel_client() as client:
        await _login(client)
        rendered = await client.get("/channels")
    assert "پاک کردن توکن این کانال" not in rendered.text


@pytest.mark.asyncio
async def test_create_with_no_token_anywhere_explains_itself(
    session: AsyncSession, stub_get_chat, monkeypatch
):
    """No token in the form and none in the environment: a clear 422, no row."""
    from app.config import settings

    monkeypatch.setattr(settings, "bale_bot_token", "")
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            "/channels", data={"platform": "bale", "target": "@tok", "bot_token": ""},
            follow_redirects=False,
        )
        assert response.status_code == 422
        assert "توکن" in response.text
    assert await _channels(session) == []
