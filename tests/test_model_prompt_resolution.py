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
    # No base_url: legacy path, global key applies.
    assert resolve_api_key(None, None) == settings.avalai_api_key
    monkeypatch.setenv("CUSTOM_TEST_KEY_ENV", "secret-value")
    assert resolve_api_key("CUSTOM_TEST_KEY_ENV", None) == "secret-value"
    # Missing env var without a base_url still falls back (shared endpoint).
    assert resolve_api_key("DEFINITELY_MISSING_ENV_VAR", None) == settings.avalai_api_key


def test_resolve_api_key_fails_loudly_with_custom_base(monkeypatch):
    """A row with its own endpoint but no usable key raises, never substitutes."""
    from app.news.generation import MissingModelKeyError

    monkeypatch.setenv("CUSTOM_TEST_KEY_ENV", "secret-value")
    assert (
        resolve_api_key("CUSTOM_TEST_KEY_ENV", "https://custom/v1") == "secret-value"
    )
    with pytest.raises(MissingModelKeyError, match="api_key_env"):
        resolve_api_key(None, "https://custom/v1")
    with pytest.raises(MissingModelKeyError, match="DEFINITELY_MISSING_ENV_VAR"):
        resolve_api_key("DEFINITELY_MISSING_ENV_VAR", "https://custom/v1")
    monkeypatch.setenv("CUSTOM_TEST_EMPTY", "")
    with pytest.raises(MissingModelKeyError):
        resolve_api_key("CUSTOM_TEST_EMPTY", "https://custom/v1")


def test_describe_model_credentials_never_contains_key(monkeypatch):
    """The panel display carries source + presence, never a key value."""
    from app.news.generation import describe_model_credentials

    class _Row:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setenv("CUSTOM_TEST_KEY_ENV", "secret-value")
    described = describe_model_credentials(
        _Row(base_url="https://custom/v1", api_key_env="CUSTOM_TEST_KEY_ENV")
    )
    assert described == {
        "base_url": "https://custom/v1",
        "key_env": "CUSTOM_TEST_KEY_ENV",
        "key_present": True,
        "uses_global_key": False,
    }
    missing = describe_model_credentials(
        _Row(base_url="https://custom/v1", api_key_env="DEFINITELY_MISSING_ENV_VAR")
    )
    assert missing["key_present"] is False
    assert "secret-value" not in str(described) + str(missing)


def test_resolve_generation_config_priority():
    """The shared worker/panel resolution: override > active > fallback."""

    class _Row:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    override_model = _Row(name="override-model", base_url=None, api_key_env=None)
    override_prompt = _Row(system_prompt="override prompt")
    active_model = _Row(name="active-model", base_url=None, api_key_env=None)
    active_prompt = _Row(system_prompt="active prompt")

    full = resolve_generation_config(override_model, override_prompt, active_model, active_prompt)
    assert full["model_name"] == "override-model"
    assert full["system_prompt"] == "override prompt"
    assert full["base_url"] == settings.avalai_base_url

    active_only = resolve_generation_config(None, None, active_model, active_prompt)
    assert active_only["model_name"] == "active-model"
    assert active_only["system_prompt"] == "active prompt"
    assert active_only["base_url"] == settings.avalai_base_url

    empty = resolve_generation_config(None, None, None, None)
    assert empty["model_name"] == settings.llm_model
    assert empty["system_prompt"] == SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_probe_model_connection_reports_failure_without_raising(monkeypatch):
    """Shared API/panel probe: missing keys and dead endpoints return, not raise."""
    ok, error = await probe_model_connection("any-model", "http://127.0.0.1:9/v1", None)
    assert ok is False
    assert "api_key_env" in error

    monkeypatch.setenv("CUSTOM_TEST_PROBE_KEY", "dummy-key-for-test")
    ok, error = await probe_model_connection(
        "any-model", "http://127.0.0.1:1/v1", "CUSTOM_TEST_PROBE_KEY"
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


def test_build_user_message_template():
    """The shared builder renders the exact documented template."""
    from app.news.generation import build_user_message

    assert build_user_message({
        "title": "T", "abstract": "A", "field": "F", "university": "U",
        "table_of_contents": None, "author": "Au", "supervisor": "Su",
    }) == (
        "Thesis metadata:\n"
        "title: T\nabstract: A\nfield: F\nuniversity: U\n"
        "table_of_contents: \nauthor: Au\nsupervisor: Su"
    )
