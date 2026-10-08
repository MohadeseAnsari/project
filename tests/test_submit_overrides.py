"""Tests for POST /submit model/prompt overrides (old shape intact)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.limits import limiter
from app.config import settings
from app.db.models import Main, Model, Prompt
from app.main import app


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Isolate submit counts: the 10/min limiter is process-global memory."""
    try:
        limiter.reset()
    except Exception:
        pass
    yield
    try:
        limiter.reset()
    except Exception:
        pass


def _payload(**extra) -> dict:
    base = {
        "shared_Secret": settings.shared_secret,
        "username": "tester",
        "title": "Thesis",
        "abstract": "Abstract",
        "field": "پزشکی",
        "university": "Tehran University",
        "table_of_contents": "1. Intro",
        "author": "Author",
        "supervisor": "Supervisor",
    }
    base.update(extra)
    return base


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_submit_stores_override_ids(session: AsyncSession):
    model = Model(name="m1", provider="openai", version="v1", is_active=False)
    prompt = Prompt(version="v1", system_prompt="prompt", is_active=False)
    session.add_all([model, prompt])
    await session.commit()

    async with _client() as client:
        response = await client.post(
            "/submit", json=_payload(model_id=model.id, prompt_id=prompt.id)
        )
    assert response.status_code == 200
    record_id = response.json()["id"]
    row = await session.get(Main, record_id)
    assert row.model_id == model.id
    assert row.prompt_id == prompt.id


@pytest.mark.asyncio
async def test_submit_old_shape_without_ids(session: AsyncSession):
    async with _client() as client:
        response = await client.post("/submit", json=_payload())
    assert response.status_code == 200
    row = await session.get(Main, response.json()["id"])
    assert row.model_id is None
    assert row.prompt_id is None


@pytest.mark.asyncio
async def test_submit_rejects_unknown_ids(session: AsyncSession):
    async with _client() as client:
        bad_model = await client.post("/submit", json=_payload(model_id=999999))
        assert bad_model.status_code == 422
        bad_prompt = await client.post("/submit", json=_payload(prompt_id=999999))
        assert bad_prompt.status_code == 422
    # No rows created for rejected overrides.
    count = (await session.execute(select(Main))).scalars().all()
    assert count == []
