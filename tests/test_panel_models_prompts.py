"""Tests for /models and /prompts management (single-active + no in-place edits)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Model, Prompt
from app.main import app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _secret_body(**extra) -> dict:
    return {"shared_Secret": settings.shared_secret, **extra}


@pytest.mark.asyncio
async def test_models_single_active_row(session: AsyncSession):
    async with _client() as client:
        first = await client.post(
            "/models",
            json=_secret_body(name="m-one", provider="openai", version="v1"),
        )
        assert first.status_code == 201
        second = await client.post(
            "/models",
            json=_secret_body(name="m-two", provider="openai", version="v1"),
        )
        assert second.status_code == 201
        activated = await client.post(
            f"/models/{second.json()['id']}/activate", json=_secret_body()
        )
        assert activated.status_code == 200
        assert activated.json()["is_active"] is True
    active = (
        await session.execute(select(Model).where(Model.is_active.is_(True)))
    ).scalars().all()
    assert len(active) == 1
    assert active[0].name == "m-two"


@pytest.mark.asyncio
async def test_models_reject_duplicate_and_direct_active_flag(session: AsyncSession):
    async with _client() as client:
        created = await client.post(
            "/models",
            json=_secret_body(name="m-dup", provider="openai", version="v1"),
        )
        assert created.status_code == 201
        duplicate = await client.post(
            "/models",
            json=_secret_body(name="m-dup", provider="openai", version="v1"),
        )
        assert duplicate.status_code == 422
        direct = await client.patch(
            f"/models/{created.json()['id']}",
            json=_secret_body(provider="other"),
        )
        assert direct.status_code == 200
        assert direct.json()["provider"] == "other"
        forbidden = await client.patch(
            f"/models/{created.json()['id']}",
            json=_secret_body(is_active=True),
        )
        assert forbidden.status_code == 422
        assert (await client.get("/models")).status_code == 401


@pytest.mark.asyncio
async def test_prompts_immutable_versions(session: AsyncSession):
    async with _client() as client:
        v1 = await client.post(
            "/prompts",
            json=_secret_body(version="v-test-1", system_prompt="first"),
        )
        assert v1.status_code == 201
        v2 = await client.post(
            "/prompts",
            json=_secret_body(version="v-test-2", system_prompt="second"),
        )
        assert v2.status_code == 201
        # No update endpoint: PATCH must 404/405, never edit in place.
        edited = await client.patch(
            f"/prompts/{v1.json()['id']}", json=_secret_body(system_prompt="x")
        )
        assert edited.status_code in (404, 405)
        activated = await client.post(
            f"/prompts/{v2.json()['id']}/activate", json=_secret_body()
        )
        assert activated.json()["is_active"] is True
    active = (
        await session.execute(select(Prompt).where(Prompt.is_active.is_(True)))
    ).scalars().all()
    assert len(active) == 1
    assert active[0].version == "v-test-2"


@pytest.mark.asyncio
async def test_model_connection_failure_path(session: AsyncSession):
    model = Model(
        name="unreachable-model-xyz",
        provider="openai",
        version="v1",
        is_active=False,
        base_url="http://127.0.0.1:1/v1",
        api_key_env="DEFINITELY_MISSING_ENV_VAR",
    )
    session.add(model)
    await session.commit()
    async with _client() as client:
        response = await client.post(
            f"/models/{model.id}/test-connection", json=_secret_body()
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["model"] == "unreachable-model-xyz"
    assert body["error"]
    # The key value never appears in the response.
    assert settings.avalai_api_key not in str(body)
