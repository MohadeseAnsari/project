"""Tests for GET /records (paginated listing for the review panel)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Main
from app.main import app


async def _record(
    session: AsyncSession,
    title: str = "Thesis",
    status: str = "review",
    field: str = "پزشکی",
    university: str = "Tehran University",
) -> Main:
    row = Main(
        username="tester",
        title=title,
        abstract="Abstract",
        field=field,
        university=university,
        author="Author",
        supervisor="Supervisor",
        status=status,
    )
    session.add(row)
    await session.commit()
    return row


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_list_records_happy(session: AsyncSession):
    await _record(session, title="First")
    await _record(session, title="Second")
    secret = settings.shared_secret
    async with _client() as client:
        response = await client.get(
            "/records", params={"shared_Secret": secret, "page": 1, "page_size": 20}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert len(body["items"]) == 2
    assert {item["title"] for item in body["items"]} == {"First", "Second"}


@pytest.mark.asyncio
async def test_list_records_requires_secret(session: AsyncSession):
    await _record(session)
    async with _client() as client:
        assert (await client.get("/records")).status_code == 401
        bad = await client.get("/records", params={"shared_Secret": "wrong"})
        assert bad.status_code == 401


@pytest.mark.asyncio
async def test_list_records_pagination(session: AsyncSession):
    for i in range(5):
        await _record(session, title=f"T{i}")
    secret = settings.shared_secret
    async with _client() as client:
        first = await client.get(
            "/records",
            params={"shared_Secret": secret, "page": 1, "page_size": 2},
        )
        second = await client.get(
            "/records",
            params={"shared_Secret": secret, "page": 2, "page_size": 2},
        )
        third = await client.get(
            "/records",
            params={"shared_Secret": secret, "page": 3, "page_size": 2},
        )
        too_big = await client.get(
            "/records",
            params={"shared_Secret": secret, "page": 1, "page_size": 101},
        )
    assert [r["title"] for r in first.json()["items"]] != [
        r["title"] for r in second.json()["items"]
    ]
    assert first.json()["total"] == 5
    assert len(first.json()["items"]) == 2
    assert len(second.json()["items"]) == 2
    assert len(third.json()["items"]) == 1
    assert too_big.status_code == 422


@pytest.mark.asyncio
async def test_list_records_filters(session: AsyncSession):
    await _record(session, title="Heart study", status="review", field="پزشکی")
    await _record(session, title="Bridge design", status="ready", field="مهندسی")
    await _record(
        session, title="Heart surgery", status="review", university="Shiraz University"
    )
    secret = settings.shared_secret
    async with _client() as client:
        by_status = await client.get(
            "/records", params={"shared_Secret": secret, "status": "ready"}
        )
        by_q = await client.get(
            "/records", params={"shared_Secret": secret, "q": "Heart"}
        )
        by_field = await client.get(
            "/records", params={"shared_Secret": secret, "field": "مهندسی"}
        )
        bad_order = await client.get(
            "/records", params={"shared_Secret": secret, "order": "random"}
        )
    assert by_status.json()["total"] == 1
    assert by_q.json()["total"] == 2
    assert by_field.json()["total"] == 1
    assert bad_order.status_code == 422
