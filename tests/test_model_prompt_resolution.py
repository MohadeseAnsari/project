"""Tests for model/prompt resolution: override > active > fallback."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Category, Field, Main, Model, Prompt
from app.news.generation import (
    probe_model_connection,
    resolve_api_key,
    resolve_base_url,
    resolve_generation_config,
    resolve_model_name,
    resolve_system_prompt,
)
from app.news.prompts import SYSTEM_PROMPT
from app.worker.worker import process_job


def test_resolve_model_name_priority():
    assert resolve_model_name("override", "active") == "override"
    assert resolve_model_name(None, "active") == "active"
    assert resolve_model_name(None, None) == settings.llm_model


def test_resolve_system_prompt_priority():
    assert resolve_system_prompt("override", "active") == "override"
    assert resolve_system_prompt(None, "active") == "active"
    assert resolve_system_prompt(None, None) == SYSTEM_PROMPT


def test_resolve_base_url_and_key_fallback(monkeypatch):
    assert resolve_base_url("https://custom/v1") == "https://custom/v1"
    assert resolve_base_url(None) == settings.avalai_base_url
    assert resolve_api_key(None) == settings.avalai_api_key
    monkeypatch.setenv("CUSTOM_TEST_KEY_ENV", "secret-value")
    assert resolve_api_key("CUSTOM_TEST_KEY_ENV") == "secret-value"
    # Missing env var falls back without ever returning the var name as a key.
    assert resolve_api_key("DEFINITELY_MISSING_ENV_VAR") == settings.avalai_api_key


def test_resolve_generation_config_priority():
    """The shared worker/panel resolution: override > active > fallback."""

    class _Row:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    override_model = _Row(name="override-model", base_url="https://override/v1", api_key_env=None)
    override_prompt = _Row(system_prompt="override prompt")
    active_model = _Row(name="active-model", base_url=None, api_key_env=None)
    active_prompt = _Row(system_prompt="active prompt")

    full = resolve_generation_config(override_model, override_prompt, active_model, active_prompt)
    assert full["model_name"] == "override-model"
    assert full["system_prompt"] == "override prompt"
    assert full["base_url"] == "https://override/v1"

    active_only = resolve_generation_config(None, None, active_model, active_prompt)
    assert active_only["model_name"] == "active-model"
    assert active_only["system_prompt"] == "active prompt"
    assert active_only["base_url"] == settings.avalai_base_url

    empty = resolve_generation_config(None, None, None, None)
    assert empty["model_name"] == settings.llm_model
    assert empty["system_prompt"] == SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_probe_model_connection_reports_failure_without_raising():
    """Shared API/panel probe: missing keys and dead endpoints return, not raise."""
    ok, error = await probe_model_connection("any-model", None, "")
    assert ok is False
    assert error == "no API key configured"

    ok, error = await probe_model_connection(
        "any-model", "http://127.0.0.1:1/v1", "dummy-key-for-test"
    )
    assert ok is False
    assert error
    assert "dummy-key-for-test" not in error


@pytest.mark.asyncio
async def test_process_job_uses_override_over_active(session: AsyncSession):
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()
    session.add(Field(name="پزشکی", normalized_name="پزشکی", category_id=cat.id))
    await session.commit()
    active_model = Model(name="active-model", provider="openai", version="v1", is_active=True)
    active_prompt = Prompt(version="v-active", system_prompt="active prompt", is_active=True)
    override_model = Model(name="override-model", provider="openai", version="v2", is_active=False)
    override_prompt = Prompt(version="v-override", system_prompt="override prompt", is_active=False)
    session.add_all([active_model, active_prompt, override_model, override_prompt])
    await session.commit()

    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
        model_id=override_model.id,
        prompt_id=override_prompt.id,
    )
    session.add(job)
    await session.commit()

    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = AsyncMock(title="T", lead="L", body="B")
        await process_job(session, job)
        _, kwargs = mock_gen.call_args
        assert kwargs["model_name"] == "override-model"
        assert kwargs["system_prompt"] == "override prompt"
    await session.refresh(job)
    # Override ids are kept for auditability, not overwritten by active rows.
    assert job.model_id == override_model.id
    assert job.prompt_id == override_prompt.id


@pytest.mark.asyncio
async def test_process_job_falls_back_to_active_then_config(session: AsyncSession):
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()
    session.add(Field(name="پزشکی", normalized_name="پزشکی", category_id=cat.id))
    await session.commit()
    active_model = Model(name="active-model", provider="openai", version="v1", is_active=True)
    active_prompt = Prompt(version="v-active", system_prompt="active prompt", is_active=True)
    session.add_all([active_model, active_prompt])
    await session.commit()

    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = AsyncMock(title="T", lead="L", body="B")
        await process_job(session, job)
        _, kwargs = mock_gen.call_args
        assert kwargs["model_name"] == "active-model"
        assert kwargs["system_prompt"] == "active prompt"
