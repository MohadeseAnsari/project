"""Tests for the explicit publishing timestamps (no new status).

Approve keeps ``status='review'``; ``approved_at``/``published_at`` tell
approved and published articles apart from unreviewed ones.
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
async def test_approve_stamps_once(session: AsyncSession):
    """First approval wins; re-approving never rewrites who/when."""
    row = await _review(session)
    async with _client() as client:
        first = await client.patch(f"/records/{row.id}", json={**_secret(), "action": "approve"})
        assert first.status_code == 200
        assert first.json()["approved_at"] is not None
        stamped = first.json()["approved_at"]
        second = await client.patch(f"/records/{row.id}", json={**_secret(), "action": "approve"})
        assert second.status_code == 200
        assert second.json()["approved_at"] == stamped
        assert second.json()["status"] == "review"


@pytest.mark.asyncio
async def test_records_approved_filter(session: AsyncSession):
    """approved=true/false splits reviewed rows; omitted means all."""
    await _review(session)
    approved = await _review(session)
    approved.approved_at = datetime.utcnow()
    await session.commit()
    async with _client() as client:
        secret = _secret()
        assert (await client.get("/records", params={**secret, "approved": "true"})).json()["total"] == 1
        assert (await client.get("/records", params={**secret, "approved": "false"})).json()["total"] == 1
        assert (await client.get("/records", params=secret)).json()["total"] == 2


@pytest.mark.asyncio
async def test_first_send_stamps_published_once(session: AsyncSession):
    """mark_sent stamps published_at on first send and never overwrites it."""
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
    assert row.published_at is not None
    stamped = row.published_at

    session.add(Delivery(main_id=row.id, channel_id=second_channel.id, status="pending"))
    await session.commit()
    claimed = await claim_next_delivery(session)
    assert await deliver_one(session, claimed, {"telegram": StubClient()}) is True
    await session.refresh(row)
    assert row.published_at == stamped
