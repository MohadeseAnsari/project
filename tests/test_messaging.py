"""Tests for outbound channel delivery: formatting, platform clients, and the
sender dispatcher's queue/retry behaviour.

The platform HTTP call is always stubbed — no test in this suite talks to Bale
or Telegram, so the suite is offline and deterministic.
"""

import asyncio
import itertools
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Category, Channel, Delivery, Field, FieldChannelRoute, Main
from app.messaging import dispatcher
from tests.routing_setup import route_all_active
from app.messaging.dispatcher import (
    channels_for_article,
    claim_next_delivery,
    deliver_one,
    ensure_pending_deliveries,
    recover_stale_deliveries,
)
from app.messaging.formatting import escape_markdown, render_article
from app.messaging.platforms import (
    BaleClient,
    PermanentDeliveryError,
    TelegramClient,
    TransientDeliveryError,
    build_client,
    configured_clients,
)

OUTPUT = {"title": "عنوان", "lead": "لید", "body": "متن کامل"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


_field_counter = itertools.count(1)


async def _field(session: AsyncSession) -> Field:
    """Create a field with a unique name (``fields.name`` is UNIQUE)."""
    cat = await session.get(Category, 1)
    if cat is None:
        cat = Category(name="پزشکی و علوم سلامت", description="Medical")
        session.add(cat)
        await session.commit()
    unique = f"field-{next(_field_counter)}"
    field = Field(name=unique, normalized_name=unique, category_id=cat.id)
    session.add(field)
    await session.commit()
    return field


async def _channel(session: AsyncSession, platform: str = "bale", chat_id: int = 100) -> Channel:
    channel = Channel(platform=platform, chat_id=chat_id, username="@newschanneltest123")
    session.add(channel)
    await session.commit()
    await _route_to_all(session)
    return channel


async def _route_to_all(session: AsyncSession) -> None:
    """Name every active channel as a destination for every category.

    Delivery is routed by category now, so a test that just wants news to reach
    somewhere has to say so. Re-seeding from BOTH ``_channel`` and ``_article``
    is deliberate: these two helpers are called in either order by different
    tests, and re-seeding is cheap, whereas seeding only once would silently
    leave whichever channel was created later unrouted.
    """
    categories = (await session.execute(select(Category))).scalars().all()
    for category in categories:
        await route_all_active(session, category)


async def _article(session: AsyncSession, field: Field | None = None, status: str = "review") -> Main:
    article = Main(
        username="tester",
        title="Thesis",
        abstract="Abstract",
        field="پزشکی",
        university="Tehran University",
        author="Author",
        supervisor="Supervisor",
        status=status,
        field_id=field.id if field else None,
        category_id=field.category_id if field else None,
        output_json=dict(OUTPUT) if status == "review" else None,
    )
    session.add(article)
    await session.commit()
    await _route_to_all(session)
    return article


class StubClient:
    """Records send attempts and replays a scripted outcome."""

    def __init__(self, outcome=None):
        self.calls: list[tuple[int, str]] = []
        self._outcome = outcome

    async def send_text(self, chat_id: int, text: str) -> int:
        self.calls.append((chat_id, text))
        # BaseException, not Exception: asyncio.CancelledError inherits from
        # BaseException and must still be raised by the stub.
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return 4242


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
    """Stands in for httpx.AsyncClient inside the platform clients."""

    response = FakeResponse(200, {"ok": True, "result": {"message_id": 77}})
    raises: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        if FakeAsyncClient.raises:
            raise FakeAsyncClient.raises
        return FakeAsyncClient.response


@pytest.fixture
def stub_http(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.raises = None
    FakeAsyncClient.response = FakeResponse(200, {"ok": True, "result": {"message_id": 77}})
    return FakeAsyncClient


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def test_render_article_has_emojis_bold_title_and_channel_footer():
    text = render_article(OUTPUT, "@newschanneltest123")
    assert text.startswith("📰 *عنوان*")
    assert "📰 لید" in text
    assert "📄 متن کامل" in text
    assert text.endswith("📢 @newschanneltest123")


def test_render_article_uses_single_asterisk_pairs_only():
    text = render_article({"title": "T"}, "@c")
    assert "*T*" in text
    assert "**" not in text


def test_render_article_omits_missing_sections():
    text = render_article({"title": "Only title"}, None)
    assert text == "📰 *Only title*"
    assert "📢" not in text


def test_render_article_escapes_markdown_in_generated_text():
    text = render_article({"title": "a*b_c", "body": "x_y"}, "@c")
    assert r"a\*b\_c" in text
    assert r"x\_y" in text


def test_render_article_returns_empty_for_missing_or_empty_output():
    assert render_article(None, "@c") == ""
    assert render_article({}, "@c") == ""
    assert render_article({"title": "  "}, "@c") == ""


def test_render_article_tolerates_non_string_values():
    text = render_article({"title": 12, "lead": None, "body": ["x"]}, "@c")
    assert text == ""


def test_render_article_truncates_over_long_body():
    text = render_article({"body": "x" * 5000}, "@c", max_length=100)
    assert len(text) <= 100
    assert text.endswith("…")


def test_escape_markdown_escapes_specials_only():
    assert escape_markdown("plain text 123") == "plain text 123"
    assert escape_markdown("*_[]()~`\\") == "\\*\\_\\[\\]\\(\\)\\~\\`\\\\"


# --------------------------------------------------------------------------
# platform clients
# --------------------------------------------------------------------------


def test_build_client_selects_platform():
    assert isinstance(build_client("bale", "t"), BaleClient)
    assert isinstance(build_client("telegram", "t"), TelegramClient)


def test_build_client_rejects_unknown_platform():
    with pytest.raises(PermanentDeliveryError):
        build_client("whatsapp", "t")


def test_build_client_requires_token():
    with pytest.raises(ValueError):
        build_client("bale", "")


@pytest.mark.asyncio
async def test_send_text_returns_message_id(stub_http):
    client = build_client("bale", "token")
    assert await client.send_text(5371341124, "hello") == 77


@pytest.mark.asyncio
async def test_send_text_marks_client_error_permanent(stub_http):
    stub_http.response = FakeResponse(400, text="chat not found")
    with pytest.raises(PermanentDeliveryError):
        await build_client("telegram", "token").send_text(1, "hi")


@pytest.mark.asyncio
async def test_send_text_marks_rate_limit_and_5xx_transient(stub_http):
    stub_http.response = FakeResponse(429, text="too many requests")
    with pytest.raises(TransientDeliveryError):
        await build_client("bale", "token").send_text(1, "hi")

    stub_http.response = FakeResponse(503, text="upstream down")
    with pytest.raises(TransientDeliveryError):
        await build_client("bale", "token").send_text(1, "hi")


@pytest.mark.asyncio
async def test_send_text_marks_timeout_transient(stub_http):
    stub_http.raises = httpx.ConnectTimeout("timed out")
    with pytest.raises(TransientDeliveryError):
        await build_client("bale", "token").send_text(1, "hi")


@pytest.mark.asyncio
async def test_ok_false_envelope_is_permanent(stub_http):
    stub_http.response = FakeResponse(200, {"ok": False, "description": "chat not found"})
    with pytest.raises(PermanentDeliveryError):
        await build_client("bale", "token").send_text(1, "hi")


@pytest.mark.asyncio
async def test_non_json_2xx_is_transient(stub_http):
    stub_http.response = FakeResponse(200, payload=None, text="<html>gateway</html>")
    with pytest.raises(TransientDeliveryError):
        await build_client("bale", "token").send_text(1, "hi")


@pytest.mark.asyncio
async def test_telegram_sends_markdown_parse_mode(monkeypatch):
    captured = {}

    class Recorder(FakeAsyncClient):
        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(200, {"ok": True, "result": {"message_id": 77}})

    monkeypatch.setattr(httpx, "AsyncClient", Recorder)
    await build_client("telegram", "tok").send_text(5, "x")

    assert captured["json"]["parse_mode"] == "Markdown"
    assert captured["url"].endswith("/bottok/sendMessage")
    assert captured["json"]["chat_id"] == 5


@pytest.mark.asyncio
async def test_bale_sends_plain_text_without_parse_mode(monkeypatch):
    captured = {}

    class Recorder(FakeAsyncClient):
        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(200, {"ok": True, "result": {"message_id": 77}})

    monkeypatch.setattr(httpx, "AsyncClient", Recorder)
    await build_client("bale", "tok").send_text(5371341124, "x")

    assert "parse_mode" not in captured["json"]
    assert captured["json"] == {"chat_id": 5371341124, "text": "x"}


def test_configured_clients_skips_platforms_without_token(monkeypatch):
    monkeypatch.setattr(settings, "bale_bot_token", "b")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    clients = configured_clients()
    assert set(clients) == {"bale"}

    monkeypatch.setattr(settings, "bale_bot_token", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    assert configured_clients() == {}


# --------------------------------------------------------------------------
# channel resolution
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_active_channel_is_a_target_even_with_a_legacy_field_route(
    session: AsyncSession,
):
    """A field->channel row no longer narrows delivery (item 6).

    It used to be the whole resolution: an article went only to the channels
    its field was routed to. Both channels now receive it, because the rule is
    "the news goes to all channels".
    """
    field = await _field(session)
    bale = await _channel(session, "bale", 100)
    telegram = await _channel(session, "telegram", 200)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=bale.id))
    await session.commit()

    article = await _article(session, field)
    channels = await channels_for_article(session, article)
    assert [c.id for c in channels] == [bale.id, telegram.id]


@pytest.mark.asyncio
async def test_channels_for_article_covers_every_active_channel(session: AsyncSession):
    field = await _field(session)
    first = await _channel(session, "bale", 100)
    second = await _channel(session, "telegram", 200)
    await session.commit()

    article = await _article(session, field)
    assert len(await channels_for_article(session, article)) == 2


@pytest.mark.asyncio
async def test_only_inactive_channels_are_excluded(session: AsyncSession):
    field = await _field(session)
    active = await _channel(session, "bale", 100)
    inactive = Channel(platform="bale", chat_id=300, is_active=False)
    session.add(inactive)
    await session.commit()

    article = await _article(session, field)
    channels = await channels_for_article(session, article)
    assert [c.id for c in channels] == [active.id]


@pytest.mark.asyncio
async def test_inactive_channel_is_never_resolved(session: AsyncSession):
    channel = Channel(platform="bale", chat_id=100, is_active=False)
    session.add(channel)
    await session.commit()
    article = await _article(session)
    assert await channels_for_article(session, article) == []


# --------------------------------------------------------------------------
# queueing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_queues_deliveries_for_review_rows(session: AsyncSession, monkeypatch):
    # Send-path test: opt out of the human gate (default True) to exercise the
    # pre-gate pending flow; the gate itself is covered in test_human_gate.py.
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    field = await _field(session)
    channel = await _channel(session)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channel.id))
    await session.commit()
    article = await _article(session, field)

    assert await ensure_pending_deliveries(session) == 1

    delivery = (await session.execute(select(Delivery))).scalars().one()
    assert delivery.main_id == article.id
    assert delivery.channel_id == channel.id
    assert delivery.status == "pending"
    assert delivery.attempts == 0


@pytest.mark.asyncio
async def test_ensure_creates_one_delivery_per_channel(session: AsyncSession):
    field = await _field(session)
    first = await _channel(session, "bale", 100)
    second = await _channel(session, "telegram", 200)
    session.add_all(
        [
            FieldChannelRoute(field_id=field.id, channel_id=first.id),
            FieldChannelRoute(field_id=field.id, channel_id=second.id),
        ]
    )
    await session.commit()
    await _article(session, field)

    assert await ensure_pending_deliveries(session) == 2


@pytest.mark.asyncio
async def test_ensure_is_idempotent(session: AsyncSession):
    field = await _field(session)
    channel = await _channel(session)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channel.id))
    await session.commit()
    await _article(session, field)

    assert await ensure_pending_deliveries(session) == 1
    assert await ensure_pending_deliveries(session) == 0
    assert len((await session.execute(select(Delivery))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_ensure_ignores_unprocessed_and_empty_rows(session: AsyncSession):
    field = await _field(session)
    channel = await _channel(session)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channel.id))
    await session.commit()

    await _article(session, field, status="processing")  # not generated yet
    empty = await _article(session, field)
    empty.output_json = {}
    await session.commit()

    assert await ensure_pending_deliveries(session) == 0


@pytest.mark.asyncio
async def test_ensure_picks_up_articles_processed_later(session: AsyncSession):
    field = await _field(session)
    channel = await _channel(session)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channel.id))
    await session.commit()

    article = await _article(session, field, status="processing")
    assert await ensure_pending_deliveries(session) == 0

    article.status = "review"
    article.output_json = dict(OUTPUT)
    await session.commit()

    assert await ensure_pending_deliveries(session) == 1


@pytest.mark.asyncio
async def test_ensure_reaches_articles_beyond_a_saturated_batch(session: AsyncSession):
    """Regression: a full batch window must not starve later articles.

    The exclusion of already-queued articles has to happen inside the query.
    Filtering in Python after LIMIT leaves the window permanently saturated by
    the first `batch_size` articles, so everything past them is never delivered
    and never recovers as the queue drains.
    """
    field = await _field(session)
    channel = await _channel(session)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channel.id))
    await session.commit()

    for _ in range(5):
        await _article(session, field)
    await session.commit()

    # Two passes of a 2-article window must cover all five.
    assert await ensure_pending_deliveries(session, batch_size=2) == 2
    assert await ensure_pending_deliveries(session, batch_size=2) == 2
    assert await ensure_pending_deliveries(session, batch_size=2) == 1

    delivered = set(
        (await session.execute(select(Delivery.main_id))).scalars().all()
    )
    every_article = set((await session.execute(select(Main.id))).scalars().all())
    assert delivered == every_article
    assert await ensure_pending_deliveries(session, batch_size=2) == 0


@pytest.mark.asyncio
async def test_ensure_returns_article_delivered_to_one_channel_but_not_another(
    session: AsyncSession, monkeypatch,
):
    """The undelivered test is per-channel, not per-article.

    An article sent to one channel but not another still needs work, so it must
    stay visible to the batch query.
    """
    # Send-path test: opt out of the human gate to assert on pending rows.
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    field = await _field(session)
    first = await _channel(session, "bale", 100)
    second = await _channel(session, "telegram", 200)
    session.add_all(
        [
            FieldChannelRoute(field_id=field.id, channel_id=first.id),
            FieldChannelRoute(field_id=field.id, channel_id=second.id),
        ]
    )
    await session.commit()

    article = await _article(session, field)
    session.add(
        Delivery(main_id=article.id, channel_id=first.id, status="sent")
    )
    await session.commit()

    assert await ensure_pending_deliveries(session) == 1
    pending = (
        await session.execute(select(Delivery).where(Delivery.channel_id == second.id))
    ).scalar_one()
    assert pending.main_id == article.id
    assert pending.status == "pending"


# --------------------------------------------------------------------------
# claiming
# --------------------------------------------------------------------------


async def _pending(session: AsyncSession, **kwargs) -> Delivery:
    """Create one article with a fresh field and a pending delivery.

    Each call uses a distinct field and reuses the existing channel (channels
    are unique per platform/chat_id), so tests can queue several deliveries
    without colliding on unique constraints.
    """
    field = await _field(session)
    channel = kwargs.pop("channel", None)
    if channel is None:
        channel = (await session.execute(select(Channel).order_by(Channel.id))).scalars().first()
    if channel is None:
        channel = await _channel(session)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channel.id))
    await session.commit()
    article = await _article(session, field)
    delivery = Delivery(main_id=article.id, channel_id=channel.id, **kwargs)
    session.add(delivery)
    await session.commit()
    return delivery


@pytest.mark.asyncio
async def test_claim_marks_sending_and_increments_attempts(session: AsyncSession):
    delivery = await _pending(session)

    claimed = await claim_next_delivery(session)
    assert claimed is not None
    assert claimed.id == delivery.id
    assert claimed.status == "sending"
    assert claimed.attempts == 1
    assert claimed.claimed_at is not None


@pytest.mark.asyncio
async def test_claim_returns_none_when_queue_empty(session: AsyncSession):
    assert await claim_next_delivery(session) is None


@pytest.mark.asyncio
async def test_claim_skips_sent_failed_and_backoff(session: AsyncSession):
    await _pending(session, status="sent")
    await _pending(session, status="failed")
    await _pending(
        session,
        status="pending",
        next_attempt_at=datetime.utcnow() + timedelta(minutes=30),
    )
    assert await claim_next_delivery(session) is None


@pytest.mark.asyncio
async def test_claim_is_fifo(session: AsyncSession):
    first = await _pending(session)
    second = await _pending(session)
    # created_at is server-defaulted; force explicit ordering.
    first.created_at = datetime(2024, 1, 1)
    second.created_at = datetime(2024, 1, 2)
    await session.commit()

    assert (await claim_next_delivery(session)).id == first.id


@pytest.mark.asyncio
async def test_awaiting_confirmation_status_is_not_claimed(session: AsyncSession):
    await _pending(session, status="awaiting_confirmation")
    assert await claim_next_delivery(session) is None


@pytest.mark.asyncio
async def test_recover_stale_deliveries(session: AsyncSession):
    stale = await _pending(session)
    stale.status = "sending"
    stale.claimed_at = datetime.utcnow() - timedelta(minutes=30)
    await session.commit()

    fresh = await _pending(session)
    fresh.status = "sending"
    fresh.claimed_at = datetime.utcnow()
    await session.commit()

    assert await recover_stale_deliveries(session) == 1
    await session.refresh(stale)
    await session.refresh(fresh)
    assert stale.status == "pending"
    assert stale.claimed_at is None
    assert fresh.status == "sending"


# --------------------------------------------------------------------------
# delivery outcome / retry policy
# --------------------------------------------------------------------------


async def _claimed(session: AsyncSession) -> Delivery:
    delivery = await _pending(session)
    return await claim_next_delivery(session)


@pytest.mark.asyncio
async def test_deliver_success_marks_sent(session: AsyncSession):
    delivery = await _claimed(session)
    client = StubClient()

    assert await deliver_one(session, delivery, {"bale": client}) is True
    await session.refresh(delivery)
    assert delivery.status == "sent"
    assert delivery.sent_at is not None
    assert delivery.platform_message_id == 4242
    assert delivery.error_message is None
    assert len(client.calls) == 1
    chat_id, text = client.calls[0]
    assert chat_id == 100
    assert text.startswith("📰 *عنوان*")
    assert text.endswith("📢 @newschanneltest123")


@pytest.mark.asyncio
async def test_transient_failure_schedules_retry_with_backoff(session: AsyncSession):
    delivery = await _claimed(session)
    client = StubClient(TransientDeliveryError("HTTP 503"))

    assert await deliver_one(session, delivery, {"bale": client}) is False
    await session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempts == 1
    assert delivery.error_message == "HTTP 503"
    assert delivery.next_attempt_at > datetime.utcnow()
    assert delivery.claimed_at is None


@pytest.mark.asyncio
async def test_backoff_grows_between_attempts():
    base = settings.sender_backoff_base_seconds
    assert dispatcher._backoff_delay(1) == base
    assert dispatcher._backoff_delay(2) == base * 2
    assert dispatcher._backoff_delay(3) == base * 4


@pytest.mark.asyncio
async def test_transient_failure_gives_up_after_max_attempts(session: AsyncSession):
    delivery = await _pending(session)
    delivery.attempts = settings.sender_max_attempts - 1
    await session.commit()
    claimed = await claim_next_delivery(session)
    assert claimed.attempts == settings.sender_max_attempts

    client = StubClient(TransientDeliveryError("still down"))
    assert await deliver_one(session, claimed, {"bale": client}) is False
    await session.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.attempts == settings.sender_max_attempts


@pytest.mark.asyncio
async def test_permanent_failure_fails_without_waiting_for_the_cap(session: AsyncSession):
    delivery = await _claimed(session)
    client = StubClient(PermanentDeliveryError("chat not found"))

    assert await deliver_one(session, delivery, {"bale": client}) is False
    await session.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.attempts == 1


@pytest.mark.asyncio
async def test_unexpected_exception_is_retried_not_fatal(session: AsyncSession):
    delivery = await _claimed(session)
    client = StubClient(RuntimeError("bug"))
    assert await deliver_one(session, delivery, {"bale": client}) is False
    await session.refresh(delivery)
    assert delivery.status == "pending"
    assert "unexpected error" in delivery.error_message


@pytest.mark.asyncio
async def test_unexpected_exception_gives_up_after_max_attempts(session: AsyncSession):
    """A bug that throws every time must not retry forever.

    The cap that stops a retryable *platform* error from looping applies to an
    unexpected exception too; otherwise one broken article re-raises indefinitely
    and never reaches `failed` for manual inspection.
    """
    delivery = await _pending(session, attempts=settings.sender_max_attempts)
    delivery = await claim_next_delivery(session)
    client = StubClient(RuntimeError("bug"))

    assert await deliver_one(session, delivery, {"bale": client}) is False
    await session.refresh(delivery)
    assert delivery.status == "failed"
    assert "unexpected error" in delivery.error_message


@pytest.mark.asyncio
async def test_missing_platform_token_fails_delivery(session: AsyncSession):
    delivery = await _claimed(session)
    assert await deliver_one(session, delivery, {}) is False
    await session.refresh(delivery)
    assert delivery.status == "failed"
    assert "no bot token" in delivery.error_message


@pytest.mark.asyncio
async def test_load_delivery_context_returns_none_for_unknown_delivery(session: AsyncSession):
    """The guard that stops the Worker retrying a delivery whose row is gone.

    The delivery cannot be orphaned in practice — the FKs cascade — but the
    lookup must still degrade to ``None`` rather than raising, so a send in
    flight during an operator cleanup cannot kill the loop.
    """
    assert await dispatcher.load_delivery_context(session, Delivery(id=999999)) is None


@pytest.mark.asyncio
async def test_empty_article_fails_without_calling_the_api(session: AsyncSession):
    delivery = await _pending(session)
    delivery_article = await session.get(Main, delivery.main_id)
    delivery_article.output_json = {"title": ""}
    await session.commit()
    claimed = await claim_next_delivery(session)

    client = StubClient()
    assert await deliver_one(session, claimed, {"bale": client}) is False
    await session.refresh(delivery)
    assert delivery.status == "failed"
    assert client.calls == []


@pytest.mark.asyncio
async def test_cancellation_leaves_delivery_for_recovery(session: AsyncSession):
    delivery = await _claimed(session)
    client = StubClient(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await deliver_one(session, delivery, {"bale": client})

    await session.refresh(delivery)
    assert delivery.status == "sending"  # reclaimed by recover_stale_deliveries


@pytest.mark.asyncio
async def test_telegram_channel_uses_telegram_client(session: AsyncSession, monkeypatch):
    # Send-path test: opt out of the human gate so the queued row is claimable.
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    field = await _field(session)
    telegram = await _channel(session, "telegram", 987)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=telegram.id))
    await session.commit()
    await _article(session, field)

    await ensure_pending_deliveries(session)
    delivery = await claim_next_delivery(session)

    client = StubClient()
    assert await deliver_one(session, delivery, {"telegram": client}) is True
    assert client.calls[0][0] == 987


# --------------------------------------------------------------------------
# end-to-end: two channels, one processed article
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_processed_article_reaches_every_routed_channel(session: AsyncSession, monkeypatch):
    # Send-path test: opt out of the human gate so queued rows are claimable.
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    field = await _field(session)
    bale = await _channel(session, "bale", 100)
    telegram = await _channel(session, "telegram", 200)
    session.add_all(
        [
            FieldChannelRoute(field_id=field.id, channel_id=bale.id),
            FieldChannelRoute(field_id=field.id, channel_id=telegram.id),
        ]
    )
    await session.commit()
    article = await _article(session, field)

    clients = {"bale": StubClient(), "telegram": StubClient()}
    for _ in range(2):
        await ensure_pending_deliveries(session)
        delivery = await claim_next_delivery(session)
        assert delivery is not None
        await deliver_one(session, delivery, clients)

    rows = (await session.execute(select(Delivery))).scalars().all()
    assert len(rows) == 2
    assert {r.status for r in rows} == {"sent"}
    assert {r.platform_message_id for r in rows} == {4242}
    assert {c for pair in clients.values() for c in [pair.calls[0][0]]} == {100, 200}

    # Nothing left to do: the article is not re-sent on a later pass.
    await ensure_pending_deliveries(session)
    assert await claim_next_delivery(session) is None
    assert len((await session.execute(select(Delivery))).scalars().all()) == 2
