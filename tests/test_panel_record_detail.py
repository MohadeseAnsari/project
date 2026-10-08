"""Tests for extended GET /records/{id} (review-panel detail payload)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Channel, Delivery, Main
from app.main import app


async def _record(session: AsyncSession) -> Main:
    row = Main(
        username="tester",
        title="Thesis",
        abstract="Abstract",
        field="پزشکی",
        university="Tehran University",
        table_of_contents="1. Intro",
        author="Author",
        supervisor="Supervisor",
        status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    return row


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_record_detail_keeps_contract_and_extends(session: AsyncSession):
    row = await _record(session)
    channel = Channel(platform="bale", chat_id=9101, username="@c")
    session.add(channel)
    await session.commit()
    session.add(
        Delivery(main_id=row.id, channel_id=channel.id, status="pending", attempts=0)
    )
    await session.commit()

    async with _client() as client:
        response = await client.get(
            f"/records/{row.id}", params={"shared_Secret": settings.shared_secret}
        )
    assert response.status_code == 200
    body = response.json()
    # Original contract fields intact.
    assert body["id"] == row.id
    assert body["status"] == "review"
    assert body["field"] == "پزشکی"
    assert body["output_json"] == {"title": "T", "lead": "L", "body": "B"}
    assert "error_message" in body
    # Extended fields.
    assert body["author"] == "Author"
    assert body["supervisor"] == "Supervisor"
    assert body["university"] == "Tehran University"
    assert body["table_of_contents"] == "1. Intro"
    assert body["created_at"] is not None
    assert body["updated_at"] is not None
    assert "field_id" in body
    assert "category_id" in body
    assert "model_id" in body
    assert "prompt_id" in body
    assert len(body["deliveries"]) == 1
    delivery = body["deliveries"][0]
    assert delivery["channel_id"] == channel.id
    assert delivery["platform"] == "bale"
    assert delivery["status"] == "pending"


@pytest.mark.asyncio
async def test_record_detail_auth_and_not_found(session: AsyncSession):
    row = await _record(session)
    async with _client() as client:
        assert (await client.get(f"/records/{row.id}")).status_code == 401
        bad = await client.get(
            f"/records/{row.id}", params={"shared_Secret": "wrong"}
        )
        assert bad.status_code == 401
        missing = await client.get(
            "/records/999999", params={"shared_Secret": settings.shared_secret}
        )
        assert missing.status_code == 404
