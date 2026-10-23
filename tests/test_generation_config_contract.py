"""The resolved generation config must fit ``generate_news`` (regression).

The Worker and the panel regenerate action both call
``generate_news(thesis, **resolve_generation_config(...))``. When a new key
(``api_model``) was added to the resolved config but not to the function
signature, every generation failed with

    TypeError: generate_news() got an unexpected keyword argument 'api_model'

and the record went to ``failed``. Existing tests mocked ``generate_news``
outright, so they never noticed. These tests exercise the real signature.
"""

import inspect

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Model, Prompt


def _accepted_kwargs() -> set[str]:
    from app.news.generation import generate_news

    return set(inspect.signature(generate_news).parameters) - {"thesis_data"}


def test_resolved_config_keys_are_all_accepted_by_generate_news():
    """Every key the resolver returns must be a parameter of generate_news.

    This is the guard that would have caught the missing ``api_model``.
    """
    from app.news.generation import resolve_generation_config

    accepted = _accepted_kwargs()

    class _Row:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    model = _Row(name="m", base_url=None, api_key_env=None, api_key_encrypted=None, api_model=None)
    prompt = _Row(system_prompt="p")
    config = resolve_generation_config(model, prompt, None, None)

    unexpected = set(config) - accepted
    assert not unexpected, (
        f"resolve_generation_config returns keys generate_news cannot accept: {sorted(unexpected)}. "
        f"generate_news accepts: {sorted(accepted)}"
    )


@pytest.mark.asyncio
async def test_worker_call_path_accepts_the_config(session: AsyncSession):
    """Same check through the real objects, not stand-ins."""
    from app.news.generation import resolve_generation_config

    model = Model(name="real-model", provider="openai", version="v1",
                  is_active=True, api_model="provider/model-v2")
    prompt = Prompt(version="v1", system_prompt="sys", is_active=True)
    session.add_all([model, prompt])
    await session.commit()

    config = resolve_generation_config(model, prompt, model, prompt)
    assert not (set(config) - _accepted_kwargs())

    # And the provider model id is what would be sent to the endpoint.
    assert config["api_model"] == "provider/model-v2"
    assert config["model_name"] == "real-model"


@pytest.mark.asyncio
async def test_generate_news_uses_api_model_over_the_display_name(monkeypatch):
    """``api_model`` wins: that is the id the provider understands."""
    from app.news import generation as gen

    captured = {}

    class _FakeModel:
        def __init__(self, name, provider=None):
            self.name = name
            captured["model"] = name

    class _FakeAgent:
        def __init__(self, model, output_type=None, system_prompt=None, model_settings=None):
            self.model = model

        async def run(self, message):
            captured["message"] = message

            class _Out:
                title = "t"
                lead = "l"
                body = "b"

            return type("R", (), {"output": _Out()})()

    monkeypatch.setattr(gen, "OpenAIChatModel", _FakeModel)
    monkeypatch.setattr(gen, "Agent", _FakeAgent)
    monkeypatch.setattr(gen, "OpenAIProvider", lambda openai_client=None: None)
    monkeypatch.setattr(gen, "AsyncOpenAI", lambda **kwargs: object())
    # Force the non-cached branch.
    monkeypatch.setattr(gen.settings, "avalai_base_url", "https://configured/v1")

    await gen.generate_news(
        {"title": "x"}, model_name="display-name", api_model="real-provider-id",
        base_url="https://other/v1", api_key="k",
    )
    assert captured["model"] == "real-provider-id", "the provider must receive the real id"


@pytest.mark.asyncio
async def test_generate_news_without_api_model_keeps_the_row_name(monkeypatch):
    """No api_model on the row: the row's name is still used."""
    from app.news import generation as gen

    captured = {}

    class _FakeModel:
        def __init__(self, name, provider=None):
            self.name = name
            captured["model"] = name

    class _FakeAgent:
        def __init__(self, model, output_type=None, system_prompt=None, model_settings=None):
            self.model = model

        async def run(self, message):
            class _Out:
                title = "t"
                lead = "l"
                body = "b"

            return type("R", (), {"output": _Out()})()

    monkeypatch.setattr(gen, "OpenAIChatModel", _FakeModel)
    monkeypatch.setattr(gen, "Agent", _FakeAgent)
    monkeypatch.setattr(gen, "OpenAIProvider", lambda openai_client=None: None)
    monkeypatch.setattr(gen, "AsyncOpenAI", lambda **kwargs: object())
    monkeypatch.setattr(gen.settings, "avalai_base_url", "https://configured/v1")

    await gen.generate_news({"title": "x"}, model_name="only-name", base_url="https://o/v1", api_key="k")
    assert captured["model"] == "only-name"


@pytest.mark.asyncio
async def test_process_job_calls_generate_news_with_the_real_config(session: AsyncSession):
    """The Worker's own call site accepts everything the resolver returns.

    Patching ``generate_news`` records the kwargs actually passed, which is
    exactly what broke before (the call site was fine; the signature was not).
    """
    from unittest.mock import AsyncMock, patch

    from app.db.models import Main
    from app.worker.worker import claim_next_job, process_job

    model = Model(name="worker-model", provider="openai", version="v1",
                  is_active=True, api_model="provider/real-id")
    # Classification no longer needs a category up front (v2-combined: the
    # combined call carries it); the seeded row here is just vocabulary.
    from app.db.models import Category

    session.add(Category(name="پزشکی و علوم سلامت", description="Medical"))
    session.add(model)
    await session.commit()
    job = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="ready",
    )
    session.add(job)
    await session.commit()

    claimed = await claim_next_job(session)
    assert claimed is not None
    from app.schemas.news import GeneratedNews

    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = GeneratedNews(title="T", lead="L", body="B", categories=[])
        await process_job(session, claimed)
    assert mock_gen.await_args is not None
    assert "api_model" in mock_gen.await_args.kwargs
    await session.refresh(claimed)
    assert claimed.status == "review", "generation completed, not failed"