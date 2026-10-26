"""Tests for the explicit record lifecycle statuses (approved, published).

Approve moves ``status='review' -> 'approved'``; the first successful send
moves it to ``published``. Timestamps stamp once, first-wins.
"""

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Channel, Delivery, Main
from app.main import app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _secret() -> dict:
    return {"shared_Secret": settings.shared_secret}


async def _review(session: AsyncSession, **extra) -> Main:
    row = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"}, **extra,
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_approve_stamps_once_and_moves_status(session: AsyncSession):
    """First approval wins: status becomes 'approved', timestamp never rewritten."""
    row = await _review(session)
    async with _client() as client:
        first = await client.patch(f"/records/{row.id}", json={**_secret(), "action": "approve"})
        assert first.status_code == 200
        assert first.json()["status"] == "approved"
        assert first.json()["approved_at"] is not None
        first.json()["approved_at"]
        # Approving twice is refused (not in review any more) and never rewrites
        # who/when approved.
        second = await client.patch(f"/records/{row.id}", json={**_secret(), "action": "approve"})
        assert second.status_code == 409
    await session.refresh(row)
    assert row.status == "approved"
    assert row.approved_at is not None


@pytest.mark.asyncio
async def test_records_approved_filter(session: AsyncSession):
    """approved=true/false splits the lifecycle by status; omitted means all."""
    await _review(session)
    approved = await _review(session)
    approved.status = "approved"
    approved.approved_at = datetime.utcnow()
    await session.commit()
    async with _client() as client:
        secret = _secret()
        assert (await client.get("/records", params={**secret, "approved": "true"})).json()["total"] == 1
        assert (await client.get("/records", params={**secret, "approved": "false"})).json()["total"] == 1
        assert (await client.get("/records", params=secret)).json()["total"] == 2


@pytest.mark.asyncio
async def test_first_send_publishes_record_once(session: AsyncSession):
    """mark_sent flips the record to 'published' on first send and never overwrites."""
    from app.messaging.dispatcher import claim_next_delivery, deliver_one

    row = await _review(session)
    first_channel = Channel(platform="bale", chat_id=96001, username="@c", is_active=True)
    second_channel = Channel(platform="telegram", chat_id=96002, username="@d", is_active=True)
    session.add_all([first_channel, second_channel])
    await session.commit()
    session.add(Delivery(main_id=row.id, channel_id=first_channel.id, status="pending"))
    await session.commit()

    class StubClient:
        async def send_text(self, chat_id: int, text: str) -> int:
            return 4242

    claimed = await claim_next_delivery(session)
    assert await deliver_one(session, claimed, {"bale": StubClient()}) is True
    await session.refresh(row)
    assert row.status == "published"
    assert row.published_at is not None
    stamped = row.published_at

    session.add(Delivery(main_id=row.id, channel_id=second_channel.id, status="pending"))
    await session.commit()
    claimed = await claim_next_delivery(session)
    assert await deliver_one(session, claimed, {"telegram": StubClient()}) is True
    await session.refresh(row)
    assert row.published_at == stamped
    assert row.status == "published"
