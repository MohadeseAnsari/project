"""Tests for GET /deliveries and POST /deliveries/{id}/retry."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Category, Channel, Delivery, Field, Main
from app.main import app


async def _setup(session: AsyncSession, platform: str = "bale", chat_id: int = 9301):
    cat = await session.get(Category, 1)
    if cat is None:
        cat = Category(name="پزشکی و علوم سلامت", description="Medical")
        session.add(cat)
        await session.commit()
    field = Field(name=f"f-{chat_id}", normalized_name=f"f-{chat_id}", category_id=cat.id)
    session.add(field)
    await session.commit()
    article = Main(
        username="tester",
        title="Thesis",
        abstract="Abstract",
        field="پزشکی",
        university="Tehran University",
        author="Author",
        supervisor="Supervisor",
        status="review",
        field_id=field.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(article)
    await session.commit()
    channel = Channel(platform=platform, chat_id=chat_id, username="@c")
    session.add(channel)
    await session.commit()
    return article, channel


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_list_deliveries_filters_and_pagination(session: AsyncSession):
    article, bale = await _setup(session, "bale", 9301)
    _, telegram = await _setup(session, "telegram", 9302)
    session.add_all(
        [
            Delivery(main_id=article.id, channel_id=bale.id, status="failed"),
            Delivery(main_id=article.id, channel_id=telegram.id, status="sent"),
        ]
    )
    await session.commit()
    secret = settings.shared_secret
    async with _client() as client:
        all_rows = await client.get(
            "/deliveries", params={"shared_Secret": secret}
        )
        assert all_rows.status_code == 200
        assert all_rows.json()["total"] == 2
        by_status = await client.get(
            "/deliveries", params={"shared_Secret": secret, "status": "failed"}
        )
        assert by_status.json()["total"] == 1
        by_platform = await client.get(
            "/deliveries", params={"shared_Secret": secret, "platform": "telegram"}
        )
        assert by_platform.json()["total"] == 1
        by_article = await client.get(
            "/deliveries",
            params={"shared_Secret": secret, "main_id": article.id},
        )
        assert by_article.json()["total"] == 2
        paged = await client.get(
            "/deliveries",
            params={"shared_Secret": secret, "page": 1, "page_size": 1},
        )
        assert len(paged.json()["items"]) == 1
        assert paged.json()["total"] == 2
        assert (await client.get("/deliveries")).status_code == 401


@pytest.mark.asyncio
async def test_retry_failed_resets_attempts(session: AsyncSession):
    article, channel = await _setup(session, "bale", 9303)
    delivery = Delivery(
        main_id=article.id,
        channel_id=channel.id,
        status="failed",
        attempts=5,
        error_message="boom",
    )
    session.add(delivery)
    await session.commit()

    async with _client() as client:
        response = await client.post(
            f"/deliveries/{delivery.id}/retry",
            json={"shared_Secret": settings.shared_secret},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    await session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempts == 0


@pytest.mark.asyncio
async def test_retry_rejects_non_retryable(session: AsyncSession):
    article, channel = await _setup(session, "bale", 9304)
    pending = Delivery(main_id=article.id, channel_id=channel.id, status="pending")
    session.add(pending)
    await session.commit()

    async with _client() as client:
        conflict = await client.post(
            f"/deliveries/{pending.id}/retry",
            json={"shared_Secret": settings.shared_secret},
        )
        assert conflict.status_code == 409
        missing = await client.post(
            "/deliveries/999999/retry",
            json={"shared_Secret": settings.shared_secret},
        )
        assert missing.status_code == 404
        bad = await client.post(
            f"/deliveries/{pending.id}/retry",
            json={"shared_Secret": "wrong"},
        )
        assert bad.status_code == 401


@pytest.mark.asyncio
async def test_release_gated_delivery(session: AsyncSession):
    """Release flips one awaiting_confirmation row to pending, keeping attempts."""
    article, channel = await _setup(session, "bale", 9305)
    gated = Delivery(main_id=article.id, channel_id=channel.id, status="awaiting_confirmation", attempts=0)
    session.add(gated)
    await session.commit()
    async with _client() as client:
        response = await client.post(
            f"/deliveries/{gated.id}/release",
            json={"shared_Secret": settings.shared_secret},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    await session.refresh(gated)
    assert gated.status == "pending"
    assert gated.attempts == 0


@pytest.mark.asyncio
async def test_release_rejects_other_states(session: AsyncSession):
    """Only gated rows release; everything else is 409, unknown is 404."""
    article, channel = await _setup(session, "bale", 9306)
    sent = Delivery(main_id=article.id, channel_id=channel.id, status="sent")
    session.add(sent)
    await session.commit()
    async with _client() as client:
        assert (await client.post(
            f"/deliveries/{sent.id}/release", json={"shared_Secret": settings.shared_secret}
        )).status_code == 409
        assert (await client.post(
            "/deliveries/999999/release", json={"shared_Secret": settings.shared_secret}
        )).status_code == 404
