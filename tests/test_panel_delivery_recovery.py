"""Delivery recovery actions over HTTP: cancel and delete-from-channel (E3).

Every stuck delivery state needs an explicit action in the panel, and each
one must actually move the row and leave an audit trail.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Channel, Delivery, Main, PanelUser, Role
from app.panel.app import panel_app
from app.services.passwords import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession, username: str = "delivery-admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


#: Channel ids are unique per (platform, chat_id); each helper call takes the next.
_next_chat_id = 9910001


async def _delivery(session: AsyncSession, status: str, attempts: int = 0) -> Delivery:
    global _next_chat_id
    row = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="approved",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    _next_chat_id += 1
    channel = Channel(
        platform="bale", chat_id=_next_chat_id, username="@c", is_active=True
    )
    session.add(channel)
    await session.commit()
    delivery = Delivery(
        main_id=row.id, channel_id=channel.id, status=status, attempts=attempts,
        platform_message_id=4242 if status == "sent" else None,
        sent_at=__import__("datetime").datetime.utcnow() if status == "sent" else None,
    )
    session.add(delivery)
    await session.commit()
    return delivery


@pytest.mark.asyncio
async def test_cancel_parked_delivery_over_http(session: AsyncSession):
    """Cancel moves a gated row to failed with an operator note, and audits."""
    await _admin(session)
    delivery = await _delivery(session, "awaiting_confirmation")
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(f"/deliveries/{delivery.id}/cancel", follow_redirects=False)
        assert response.status_code == 302
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "failed"
    assert "cancelled" in delivery.error_message
    actions = (await session.execute(
        select(AuditLog.action).where(AuditLog.main_id == delivery.main_id)
    )).scalars().all()
    assert "cancel" in actions


@pytest.mark.asyncio
async def test_cancel_refuses_in_flight_or_sent(session: AsyncSession):
    """A send already in flight, or already delivered, cannot be cancelled."""
    await _admin(session)
    sending = await _delivery(session, "sending")
    sent = await _delivery(session, "sent")
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        assert (await client.post(f"/deliveries/{sending.id}/cancel", follow_redirects=False)).status_code == 409
        assert (await client.post(f"/deliveries/{sent.id}/cancel", follow_redirects=False)).status_code == 409
    session.expire_all()
    await session.refresh(sending)
    await session.refresh(sent)
    assert sending.status == "sending", "an in-flight send is left alone"
    assert sent.status == "sent", "a published message is not 'cancelled', it is deleted from the channel"


@pytest.mark.asyncio
async def test_cancel_allows_queued_but_not_yet_sent(session: AsyncSession):
    """A queued delivery can be cancelled: nothing has reached the platform."""
    await _admin(session)
    pending = await _delivery(session, "pending")
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        assert (await client.post(f"/deliveries/{pending.id}/cancel", follow_redirects=False)).status_code == 302
    session.expire_all()
    await session.refresh(pending)
    assert pending.status == "failed"
    # And the sender must no longer pick it up.
    from app.messaging.dispatcher import claim_next_delivery

    assert await claim_next_delivery(session) is None


@pytest.mark.asyncio
async def test_delete_from_channel_calls_the_platform_and_records_it(session: AsyncSession, monkeypatch):
    """Deleting really calls the channel's deleteMessage, then records the state.

    The status change alone would be a lie: the owner asked for the message to
    be gone from the channel, so the platform call is the thing under test.
    """
    from app.messaging import platforms

    await _admin(session)
    delivery = await _delivery(session, "sent")
    channel_id = delivery.channel_id
    calls: list[tuple[int, int]] = []

    class _Recording:
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            calls.append((chat_id, message_id))
            return True

    monkeypatch.setattr(
        platforms, "client_for_channel", lambda channel, clients: (_Recording(), None)
    )
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/deliveries/{delivery.id}/delete-from-channel", follow_redirects=False
        )
        assert response.status_code == 303
    assert len(calls) == 1, "the platform must be asked to delete the message"
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "deleted_from_channel"
    assert delivery.platform_message_id == 4242, "history keeps the platform message id"
    assert calls[0][1] == 4242, "the message the platform sent is the one removed"
    detail = (
        await session.execute(
            select(AuditLog.detail).where(AuditLog.action == "delete_from_channel")
        )
    ).scalars().all()
    assert detail and "4242" in detail[0]
    assert channel_id == delivery.channel_id


@pytest.mark.asyncio
async def test_delete_from_channel_refusal_keeps_the_row_sent(session: AsyncSession, monkeypatch):
    """A platform refusal leaves the message published and says so (409)."""
    from app.messaging import platforms
    from app.messaging.platforms import PermanentDeliveryError

    await _admin(session)
    delivery = await _delivery(session, "sent")

    class _Refusing:
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            # A real refusal: the message is still there, the bot has no rights.
            raise PermanentDeliveryError("deleteMessage: not enough rights")

    monkeypatch.setattr(
        platforms, "client_for_channel", lambda channel, clients: (_Refusing(), None)
    )
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/deliveries/{delivery.id}/delete-from-channel", follow_redirects=False
        )
        assert response.status_code == 409
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "sent", "the message is still in the channel, so the row must not lie"
    actions = (await session.execute(
        select(AuditLog.action).where(AuditLog.action == "delete_from_channel_failed")
    )).scalars().all()
    assert actions, "a failed delete is audited too"


@pytest.mark.asyncio
async def test_delete_from_channel_only_for_sent(session: AsyncSession):
    """An unsent delivery cannot be 'deleted from channel' (409)."""
    await _admin(session)
    delivery = await _delivery(session, "awaiting_confirmation")
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/deliveries/{delivery.id}/delete-from-channel", follow_redirects=False
        )
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_mark_deleted_records_without_touching_the_platform(session: AsyncSession, monkeypatch):
    """The companion action for a message already removed by hand: no call out."""
    from app.messaging import platforms

    await _admin(session)
    delivery = await _delivery(session, "sent")

    def _never(channel, clients):
        raise AssertionError("mark-deleted must not call the platform")

    monkeypatch.setattr(platforms, "client_for_channel", _never)
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/deliveries/{delivery.id}/mark-deleted", follow_redirects=False
        )
        assert response.status_code == 303
    session.expire_all()
    await session.refresh(delivery)
    assert delivery.status == "deleted_from_channel"
    assert delivery.platform_message_id == 4242


@pytest.mark.asyncio
async def test_panel_offers_recovery_actions_per_state(session: AsyncSession):
    """The deliveries table shows the action that fits each state."""
    await _admin(session)
    gated = await _delivery(session, "awaiting_confirmation")
    sent = await _delivery(session, "sent")
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        table = await client.get("/deliveries/table")
    assert f"/deliveries/{gated.id}/cancel" in table.text
    assert f"/deliveries/{gated.id}/release" in table.text
    assert f"/deliveries/{sent.id}/delete-from-channel" in table.text
    # A sent row is not offered "cancel": deleting the message is the action.
    assert f"/deliveries/{sent.id}/cancel" not in table.text
    # Destructive actions ask for confirmation before firing.
    assert "confirm(" in table.text


@pytest.mark.asyncio
async def test_viewer_cannot_use_recovery_actions(session: AsyncSession):
    """Recovery actions follow the same role rule as every other mutation."""
    role = (await session.execute(select(Role).where(Role.name == "viewer"))).scalar_one_or_none()
    if role is None:
        role = Role(name="viewer", description="viewer")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="delivery-viewer", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()
    delivery = await _delivery(session, "pending")
    async with panel_client() as client:
        await client.post("/login", data={"username": "delivery-viewer", "password": "secret123"},
                          follow_redirects=False)
        assert (await client.post(f"/deliveries/{delivery.id}/cancel", follow_redirects=False)).status_code == 403
    await session.refresh(delivery)
    assert delivery.status == "pending"


@pytest.mark.asyncio
async def test_csrf_required_for_recovery_actions(session: AsyncSession):
    """Recovery actions are covered by CSRF like every other POST."""
    await _admin(session)
    delivery = await _delivery(session, "pending")
    async with AsyncClient(transport=ASGITransport(app=panel_app), base_url="http://test") as client:
        await client.post("/login", data={"username": "delivery-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(f"/deliveries/{delivery.id}/cancel", follow_redirects=False)
    assert response.status_code == 400
    await session.refresh(delivery)
    assert delivery.status == "pending"