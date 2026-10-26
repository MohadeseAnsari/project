"""Deleting an article removes its message from EVERY channel at once (PART B).

An article fans out to one delivery row per routed channel, so "delete the
news" is a bulk action. The rules these tests pin down:

* every channel is really contacted (``deleteMessage`` per channel);
* channels are independent — one refusal must not stop the others;
* a refused row stays ``sent`` (the message is still out there) and the
  successful ones are recorded as deleted;
* nothing happens without an explicit confirmation, and a viewer cannot do it.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Channel, Delivery, Main, PanelUser, Role
from app.panel.auth import hash_password
from app.services import deliveries as delivery_service
from tests.panel_test_client import panel_client


class _Platform:
    """Stands in for every platform client, keyed by message id.

    ``refuse_for`` is a REAL refusal (message still there, cannot delete it);
    ``gone_for`` is the platform saying the message does not exist any more.
    """

    def __init__(self, refuse_for: set[int] | None = None,
                 unavailable_for: set[int] | None = None, gone_for: set[int] | None = None):
        self.refuse_for = refuse_for or set()
        self.unavailable_for = unavailable_for or set()
        self.gone_for = gone_for or set()
        self.called: list[int] = []

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        from app.messaging.platforms import (
            MessageGoneError,
            PermanentDeliveryError,
            TransientDeliveryError,
        )

        self.called.append(message_id)
        if message_id in self.gone_for:
            raise MessageGoneError("deleteMessage: message to delete not found")
        if message_id in self.refuse_for:
            raise PermanentDeliveryError("deleteMessage: not enough rights")
        if message_id in self.unavailable_for:
            raise TransientDeliveryError("deleteMessage: timeout")
        return True


def _install(monkeypatch, platform: _Platform) -> None:
    from app.messaging import platforms

    monkeypatch.setattr(
        platforms, "client_for_channel", lambda channel, clients: (platform, None)
    )


async def _admin(session: AsyncSession, username: str = "fan-admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _published(session: AsyncSession, chat_ids: list[int], status: str = "sent"):
    """One article fanned out to several channels, all already sent."""
    article = Main(
        username="t", title="Fan Out", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="published",
        output_json={"title": "Fan Out", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    rows = []
    for index, chat_id in enumerate(chat_ids):
        channel = Channel(platform="bale", chat_id=chat_id, username=f"@c{index}", is_active=True)
        session.add(channel)
        await session.commit()
        row = Delivery(main_id=article.id, channel_id=channel.id, status=status,
                       platform_message_id=1000 + index)
        session.add(row)
        await session.commit()
        rows.append(row)
    return article, rows


async def _statuses(session: AsyncSession, rows) -> list[str]:
    """Re-read each row: the panel writes with its own session."""
    out = []
    for row in rows:
        await session.refresh(row)
        out.append(row.status)
    return out


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_channel_is_actually_contacted(session: AsyncSession, monkeypatch):
    """One action, one real deleteMessage call per channel the article reached."""
    platform = _Platform()
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71001, 71002, 71003])
    report = await delivery_service.delete_article_everywhere(session, article.id)
    assert len(platform.called) == 3
    assert platform.called == [1000, 1001, 1002]
    assert len(report["deleted"]) == 3
    assert report["failed"] == []
    assert await _statuses(session, rows) == ["deleted_from_channel"] * 3


@pytest.mark.asyncio
async def test_a_message_that_is_already_gone_counts_as_done(
    session: AsyncSession, monkeypatch
):
    """A "message not found" is success, not a refusal to retry forever.

    The message is not published — the exact postcondition the operator asked
    for. Treating it as a failure would leave the row permanently `sent` for a
    message that is no longer in the channel.
    """
    platform = _Platform(gone_for={1001})
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71005, 71006])
    report = await delivery_service.delete_article_everywhere(session, article.id)
    assert report["already_gone"] == ["@c1"]
    assert report["failed"] == []
    assert report["deleted"] == ["@c0"]
    assert await _statuses(session, rows) == ["deleted_from_channel"] * 2


@pytest.mark.asyncio
async def test_already_gone_is_not_a_failure_for_the_single_route(
    session: AsyncSession, monkeypatch
):
    """The one-channel action treats an absent message as done too (303, not 409)."""
    platform = _Platform(gone_for={1000})
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71007])
    await delivery_service.delete_published_message(session, rows[0])
    assert await _statuses(session, rows) == ["deleted_from_channel"]


@pytest.mark.asyncio
async def test_one_refusal_does_not_stop_the_others(session: AsyncSession, monkeypatch):
    """Channels are independent: a refusal is reported, the rest still go."""
    platform = _Platform(refuse_for={1001})
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71011, 71012, 71013])
    report = await delivery_service.delete_article_everywhere(session, article.id)
    assert len(report["deleted"]) == 2
    assert len(report["failed"]) == 1
    channel_name, code = report["failed"][0]
    assert code.startswith("delete-refused")
    assert isinstance(channel_name, str) and channel_name
    statuses = await _statuses(session, rows)
    assert statuses == ["deleted_from_channel", "sent", "deleted_from_channel"], (
        "the refused row must stay sent: that message is still in its channel"
    )


@pytest.mark.asyncio
async def test_a_transport_failure_is_reported_as_retryable(session: AsyncSession, monkeypatch):
    platform = _Platform(unavailable_for={1000})
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71021, 71022])
    report = await delivery_service.delete_article_everywhere(session, article.id)
    assert [code for _, code in report["failed"]][0].startswith("delete-unavailable")
    assert await _statuses(session, rows) == ["sent", "deleted_from_channel"]


@pytest.mark.asyncio
async def test_channels_that_were_never_published_are_skipped(session: AsyncSession, monkeypatch):
    """A pending or already-deleted row is left alone, and not even called."""
    platform = _Platform()
    _install(monkeypatch, platform)
    article = Main(
        username="t", title="Mixed", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="published",
        output_json={"title": "Mixed", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    channel_a = Channel(platform="bale", chat_id=71031, username="@a", is_active=True)
    channel_b = Channel(platform="bale", chat_id=71032, username="@b", is_active=True)
    session.add_all([channel_a, channel_b])
    await session.commit()
    sent = Delivery(main_id=article.id, channel_id=channel_a.id, status="sent",
                    platform_message_id=2000)
    pending = Delivery(main_id=article.id, channel_id=channel_b.id, status="pending")
    session.add_all([sent, pending])
    await session.commit()

    report = await delivery_service.delete_article_everywhere(session, article.id)
    assert report["deleted"] == ["@a"]
    assert report["skipped"] == ["@b"]
    assert len(platform.called) == 1
    assert (await session.get(Delivery, pending.id)).status == "pending"


@pytest.mark.asyncio
async def test_article_with_nothing_published_reports_nothing(session: AsyncSession, monkeypatch):
    platform = _Platform()
    _install(monkeypatch, platform)
    article, _rows = await _published(session, [71041], status="pending")
    report = await delivery_service.delete_article_everywhere(session, article.id)
    assert report["deleted"] == [] and report["failed"] == []
    assert report["skipped"] == ["@c0"]
    assert platform.called == []


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_action_deletes_from_every_channel(session: AsyncSession, monkeypatch):
    await _admin(session)
    platform = _Platform()
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71051, 71052, 71053])
    async with panel_client() as client:
        await client.post("/login", data={"username": "fan-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/records/{article.id}/delete-everywhere", data={"confirm": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert len(platform.called) == 3
    assert await _statuses(session, rows) == ["deleted_from_channel"] * 3
    detail = (await session.execute(
        select(AuditLog.detail).where(AuditLog.action == "delete_from_channel")
    )).scalars().all()
    assert detail and "deleted=3" in detail[0] and "failed=0" in detail[0]

@pytest.mark.asyncio
async def test_partial_failure_is_shown_on_the_page_and_audited(
    session: AsyncSession, monkeypatch
):
    await _admin(session)
    _install(monkeypatch, _Platform(refuse_for={1001}))
    article, rows = await _published(session, [71061, 71062, 71063])
    async with panel_client() as client:
        await client.post("/login", data={"username": "fan-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/records/{article.id}/delete-everywhere", data={"confirm": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = await client.get(response.headers["location"])
    assert await _statuses(session, rows) == ["deleted_from_channel", "sent", "deleted_from_channel"]
    assert "بعضی کانال‌ها" in page.text, "the operator is told the delete was partial"
    failures = (await session.execute(
        select(AuditLog.detail).where(AuditLog.action == "delete_from_channel_failed")
    )).scalars().all()
    assert failures, "each failed channel is audited with its reason"
    assert "delete-refused" in failures[0]


@pytest.mark.asyncio
async def test_button_appears_only_when_something_is_published(
    session: AsyncSession, monkeypatch
):
    await _admin(session)
    _install(monkeypatch, _Platform())
    article, _rows = await _published(session, [71071, 71072])
    async with panel_client() as client:
        await client.post("/login", data={"username": "fan-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get(f"/records/{article.id}?tab=delivery")
        assert f"/records/{article.id}/delete-everywhere" in page.text
        assert "حذف پیام از همهٔ کانال‌ها" in page.text
        assert "confirm(" in page.text, "a bulk irreversible delete confirms first"
    await delivery_service.delete_article_everywhere(session, article.id)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fan-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get(f"/records/{article.id}?tab=delivery")
    assert f"/records/{article.id}/delete-everywhere" not in page.text, (
        "nothing left to delete, so no button"
    )


@pytest.mark.asyncio
async def test_without_confirmation_nothing_is_deleted(session: AsyncSession, monkeypatch):
    """A stray click must not remove a message from every channel."""
    await _admin(session)
    platform = _Platform()
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71081, 71082])
    async with panel_client() as client:
        await client.post("/login", data={"username": "fan-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/records/{article.id}/delete-everywhere", data={}, follow_redirects=False
        )
        assert response.status_code == 303
    assert platform.called == []
    assert await _statuses(session, rows) == ["sent", "sent"]


@pytest.mark.asyncio
async def test_viewer_cannot_bulk_delete(session: AsyncSession, monkeypatch):
    role = (await session.execute(select(Role).where(Role.name == "viewer"))).scalar_one_or_none()
    if role is None:
        role = Role(name="viewer", description="viewer")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="fan-viewer", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()
    platform = _Platform()
    _install(monkeypatch, platform)
    article, rows = await _published(session, [71091, 71092])
    async with panel_client() as client:
        await client.post("/login", data={"username": "fan-viewer", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/records/{article.id}/delete-everywhere", data={"confirm": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 403
    assert platform.called == []
    assert await _statuses(session, rows) == ["sent", "sent"]


@pytest.mark.asyncio
async def test_unknown_record_is_a_404(session: AsyncSession, monkeypatch):
    await _admin(session)
    _install(monkeypatch, _Platform())
    async with panel_client() as client:
        await client.post("/login", data={"username": "fan-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            "/records/999999/delete-everywhere", data={"confirm": "yes"}, follow_redirects=False
        )
        assert response.status_code == 404
