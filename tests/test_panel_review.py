"""Tests for PATCH /records/{id} (human review actions)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Channel, Delivery, Main
from app.main import app


async def _review(session: AsyncSession, status: str = "review") -> Main:
    row = Main(
        username="tester",
        title="Thesis",
        abstract="Abstract",
        field="پزشکی",
        university="Tehran University",
        author="Author",
        supervisor="Supervisor",
        status=status,
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    return row


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _payload(action: str, **extra) -> dict:
    return {"shared_Secret": settings.shared_secret, "action": action, **extra}


@pytest.mark.asyncio
async def test_approve_flips_gated_deliveries(session: AsyncSession):
    row = await _review(session)
    channel = Channel(platform="bale", chat_id=9201, username="@c")
    session.add(channel)
    await session.commit()
    session.add(
        Delivery(
            main_id=row.id, channel_id=channel.id, status="awaiting_confirmation"
        )
    )
    await session.commit()

    async with _client() as client:
        response = await client.patch(f"/records/{row.id}", json=_payload("approve"))
    assert response.status_code == 200
    assert response.json()["status"] == "review"
    delivery = (
        await session.execute(select(Delivery).where(Delivery.main_id == row.id))
    ).scalar_one()
    assert delivery.status == "pending"


@pytest.mark.asyncio
async def test_reject_moves_to_rejected(session: AsyncSession):
    row = await _review(session)
    async with _client() as client:
        response = await client.patch(f"/records/{row.id}", json=_payload("reject"))
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    await session.refresh(row)
    assert row.status == "rejected"


@pytest.mark.asyncio
async def test_save_draft_edits_output(session: AsyncSession):
    row = await _review(session)
    async with _client() as client:
        response = await client.patch(
            f"/records/{row.id}", json=_payload("save_draft", title="New title")
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "review"
    assert body["output_json"]["title"] == "New title"
    assert body["output_json"]["lead"] == "L"


@pytest.mark.asyncio
async def test_patch_only_from_review(session: AsyncSession):
    row = await _review(session, status="ready")
    async with _client() as client:
        conflict = await client.patch(f"/records/{row.id}", json=_payload("approve"))
        assert conflict.status_code == 409
        missing = await client.patch("/records/999999", json=_payload("approve"))
        assert missing.status_code == 404
        bad_secret = await client.patch(
            f"/records/{row.id}",
            json={"shared_Secret": "wrong", "action": "approve"},
        )
        assert bad_secret.status_code == 401
        bad_action = await client.patch(
            f"/records/{row.id}", json=_payload("publish")
        )
        assert bad_action.status_code == 422
