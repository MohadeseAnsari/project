"""``deleteMessage`` on the platform clients, and what the panel does with it.

Two layers are covered:

* the client really calls the platform's ``deleteMessage`` with the chat and
  the message id it was given, and classifies a refusal as permanent;
* the service only records ``deleted_from_channel`` AFTER the platform
  confirms, and leaves the row ``sent`` (with a reason) when it does not.
"""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, Delivery, Main
from app.services import deliveries as delivery_service


class _Result:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id


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
    """Stands in for httpx.AsyncClient and records POSTed methods/payloads."""

    status = 200
    payload = {"ok": True, "result": True}
    raises: Exception | None = None
    last_post: tuple | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        raise AssertionError("delete tests must not GET")

    async def post(self, url, json=None):
        FakeAsyncClient.last_post = (url, json)
        if FakeAsyncClient.raises:
            raise FakeAsyncClient.raises
        if FakeAsyncClient.status != 200:
            return FakeResponse(FakeAsyncClient.status, text="message to delete not found")
        return FakeResponse(200, FakeAsyncClient.payload)


@pytest.fixture
def stub_post(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.status = 200
    FakeAsyncClient.raises = None
    FakeAsyncClient.payload = {"ok": True, "result": True}
    FakeAsyncClient.last_post = None
    return FakeAsyncClient


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["bale", "telegram"])
async def test_delete_message_calls_the_platform(stub_post, platform: str):
    """The published message is removed with the chat and id it was sent to."""
    from app.messaging.platforms import build_client

    client = build_client(platform, "token")
    assert await client.delete_message(123456, 789) is True
    url, payload = stub_post.last_post
    assert url.endswith("/bottoken/deleteMessage")
    assert payload == {"chat_id": 123456, "message_id": 789}


@pytest.mark.asyncio
async def test_delete_message_refusal_is_permanent(stub_post):
    """A refusal (too old / no rights) must not look like a transient hiccup."""
    from app.messaging.platforms import PermanentDeliveryError, build_client

    stub_post.status = 400
    with pytest.raises(PermanentDeliveryError):
        await build_client("bale", "token").delete_message(1, 2)


@pytest.mark.asyncio
async def test_delete_message_transport_failure_is_retryable(stub_post):
    from app.messaging.platforms import TransientDeliveryError, build_client

    stub_post.raises = httpx.ConnectTimeout("timed out")
    with pytest.raises(TransientDeliveryError):
        await build_client("bale", "token").delete_message(1, 2)


@pytest.fixture(autouse=True)
def _no_platform_token(monkeypatch):
    """No ambient bot token: each test states its own, so nothing is implicit."""
    from app.config import settings

    monkeypatch.setattr(settings, "bale_bot_token", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")


async def _sent_delivery(session: AsyncSession, message_id: int | None = 4242,
                         channel_token: str | None = None) -> Delivery:
    article = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U", author="A",
        supervisor="S", status="published", output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    channel = Channel(platform="bale", chat_id=8888, username="@del", is_active=True)
    if channel_token:
        from app.credentials import encrypt

        channel.bot_token_encrypted = encrypt(channel_token)
    session.add(channel)
    await session.commit()
    delivery = Delivery(
        main_id=article.id, channel_id=channel.id, status="sent",
        platform_message_id=message_id,
    )
    session.add(delivery)
    await session.commit()
    return delivery


@pytest.mark.asyncio
async def test_service_records_the_state_only_after_the_platform_agrees(
    session: AsyncSession, stub_post
):
    """The happy path: platform confirms, then the row moves."""
    delivery = await _sent_delivery(session, channel_token="7788:SENDER-TOKEN")
    await delivery_service.delete_published_message(session, delivery)
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "deleted_from_channel"
    assert delivery.platform_message_id == 4242, "the row is the history and keeps the id"
    _, payload = stub_post.last_post
    assert payload == {"chat_id": 8888, "message_id": 4242}


@pytest.mark.asyncio
async def test_refused_delete_keeps_the_row_sent(session: AsyncSession, stub_post):
    """A refusal must not fake a deletion: the row stays ``sent``."""
    delivery = await _sent_delivery(session, channel_token="7788:SENDER-TOKEN")
    stub_post.status = 400
    with pytest.raises(ValueError) as excinfo:
        await delivery_service.delete_published_message(session, delivery)
    assert str(excinfo.value).startswith("delete-refused")
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "sent", "the message is still in the channel"
    assert delivery.platform_message_id == 4242


@pytest.mark.asyncio
async def test_transport_failure_is_reported_as_retryable(session: AsyncSession, stub_post):
    delivery = await _sent_delivery(session, channel_token="7788:SENDER-TOKEN")
    stub_post.raises = httpx.ConnectTimeout("timed out")
    with pytest.raises(ValueError) as excinfo:
        await delivery_service.delete_published_message(session, delivery)
    assert str(excinfo.value).startswith("delete-unavailable")
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "sent"


@pytest.mark.asyncio
async def test_falls_back_to_the_platform_wide_token(session: AsyncSession, stub_post, monkeypatch):
    """A channel with no token of its own is deleted with the platform token."""
    from app.config import settings

    monkeypatch.setattr(settings, "bale_bot_token", "PLATFORM-WIDE-TOKEN")
    delivery = await _sent_delivery(session)
    await delivery_service.delete_published_message(session, delivery)
    url, _ = stub_post.last_post
    assert "botPLATFORM-WIDE-TOKEN/deleteMessage" in url


@pytest.mark.asyncio
async def test_uses_the_channels_own_token(session: AsyncSession, stub_post, monkeypatch):
    """The channel's own token beats the platform-wide one for its own messages."""
    from app.config import settings

    monkeypatch.setattr(settings, "bale_bot_token", "PLATFORM-WIDE-TOKEN")
    delivery = await _sent_delivery(session, channel_token="7777:CHANNEL-OWN-TOKEN")
    await delivery_service.delete_published_message(session, delivery)
    url, _ = stub_post.last_post
    assert "bot7777:CHANNEL-OWN-TOKEN/deleteMessage" in url


@pytest.mark.asyncio
async def test_without_any_token_it_fails_loudly(session: AsyncSession, stub_post):
    """No token means no deletion — and the row is not touched."""
    delivery = await _sent_delivery(session)
    with pytest.raises(ValueError) as excinfo:
        await delivery_service.delete_published_message(session, delivery)
    assert str(excinfo.value).startswith("no-token")
    assert "PLATFORM" not in str(excinfo.value), "the reason never carries a token value"
    assert stub_post.last_post is None, "nothing was sent to any platform"
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "sent"


@pytest.mark.asyncio
async def test_missing_message_id_is_refused(session: AsyncSession, stub_post):
    """A row with no platform id cannot be deleted, and says why."""
    delivery = await _sent_delivery(session, message_id=None)
    with pytest.raises(ValueError) as excinfo:
        await delivery_service.delete_published_message(session, delivery)
    assert str(excinfo.value) == "missing-message-id"
    assert stub_post.last_post is None


@pytest.mark.asyncio
async def test_mark_deleted_does_not_talk_to_the_platform(session: AsyncSession, stub_post):
    """The manual action records what the operator did, sending nothing."""
    delivery = await _sent_delivery(session)
    await delivery_service.mark_deleted_from_channel(session, delivery)
    assert stub_post.last_post is None
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "deleted_from_channel"
