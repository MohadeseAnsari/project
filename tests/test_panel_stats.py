"""Tests for GET /stats (counts plus worker heartbeat freshness)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Channel, Delivery, Main
from app.main import app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_stats_counts_and_heartbeat(session: AsyncSession):
    session.add_all(
        [
            Main(
                username="t",
                title="A",
                abstract="B",
                field="پزشکی",
                university="U",
                author="A",
                supervisor="S",
                status="review",
                output_json={"title": "T", "lead": "L", "body": "B"},
            ),
            Main(
                username="t",
                title="C",
                abstract="D",
                field="پزشکی",
                university="U",
                author="A",
                supervisor="S",
                status="ready",
            ),
        ]
    )
    await session.commit()
    channel = Channel(platform="bale", chat_id=9401, username="@c")
    session.add(channel)
    await session.commit()
    article = Main(
        username="t",
        title="E",
        abstract="F",
        field="پزشکی",
        university="U",
        author="A",
        supervisor="S",
        status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="failed"))
    await session.commit()

    async with _client() as client:
        response = await client.get(
            "/stats", params={"shared_Secret": settings.shared_secret}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["records"]["review"] == 2
    assert body["records"]["ready"] == 1
    assert body["deliveries"]["failed"] == 1
    # Freshness key present; no per-record timing fields leaked.
    assert "worker_last_heartbeat_at" in body
    assert "output_json" not in body


@pytest.mark.asyncio
async def test_stats_requires_secret(session: AsyncSession):
    async with _client() as client:
        assert (await client.get("/stats")).status_code == 401
