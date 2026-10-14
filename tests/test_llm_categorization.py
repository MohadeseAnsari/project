"""Optional LLM categorisation, OFF by default (item 20).

The owner's requirement is explicit about the default:
``USE_LLM_FOR_CATEGORIZATION=0`` uses the deterministic rules, ``=1`` asks the
model. These tests pin the half that matters most — the flag off must behave
exactly as before — plus the promise that the LLM path can only ADD a
category, never break the pipeline.

The decisive property is that NO failure of the LLM path can stop a thesis
from being processed: every one of them falls back to the rules.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Field
from app.fields import llm_classification
from app.fields.classification import classify_with_reason
from app.fields.normalization import normalize_field

CATEGORIES = ["پزشکی و علوم سلامت", "فنی و مهندسی", "علوم اجتماعی و اقتصاد", "other"]


class _FakeCompletions:
    def __init__(self, reply, error=None):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        message = type("M", (), {"content": self.reply})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice]})()


class _FakeOpenAI:
    def __init__(self, reply=None, error=None, **kwargs):
        self.completions = _FakeCompletions(reply, error)
        self.chat = type("Chat", (), {"completions": self.completions})()


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a fake AsyncOpenAI and a usable generation config.

    The fake is a CLASS, not a factory function: ``app.news.generation`` has a
    module-level ``_client: AsyncOpenAI | None`` annotation, which is evaluated
    at import time and breaks if the name is replaced by a plain function.
    """
    state = {"reply": None, "error": None, "instance": None}

    class _PatchedOpenAI:
        def __init__(self, **kwargs):
            instance = _FakeOpenAI(reply=state["reply"], error=state["error"], **kwargs)
            state["instance"] = instance
            self.completions = instance.completions
            self.chat = instance.chat

    async def _config(**_kwargs):
        return {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "model_name": "display-name",
            "api_model": "real-provider-model-id",
        }

    # Import first, THEN patch, so the module-level annotation is already
    # evaluated against the real class.
    import app.news.generation  # noqa: F401
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _PatchedOpenAI)
    monkeypatch.setattr(app.news.generation, "resolve_generation_config", _config)
    return state


# --------------------------------------------------------------------------
# the default: the flag is off
# --------------------------------------------------------------------------


def test_the_flag_defaults_to_off():
    """The deterministic path is the default, not the exception."""
    from app.config import settings

    assert settings.use_llm_for_categorization is False


@pytest.mark.asyncio
async def test_with_the_flag_off_no_provider_call_is_made(fake_llm):
    assert await llm_classification.classify_with_llm("some new field", CATEGORIES) is None
    assert fake_llm["instance"] is None, "no client was even constructed"


def test_the_rules_are_the_default_path():
    """With the flag off, a known field is placed by the rules as before."""
    name, keyword, _ = classify_with_reason(normalize_field("هوش مصنوعی"))
    assert name, "the rules still answer"
    assert keyword is not None, "and still explain themselves with a keyword"


# --------------------------------------------------------------------------
# the flag on: the LLM path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_the_flag_on_the_model_is_asked(fake_llm, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    fake_llm["reply"] = json.dumps({"category": "فنی و مهندسی"})
    chosen = await llm_classification.classify_with_llm("some new field", CATEGORIES)
    assert chosen == "فنی و مهندسی"
    call = fake_llm["instance"].completions.calls[0]
    assert call["model"] == "real-provider-model-id", (
        "the provider's real model id must be sent, not the display name"
    )
    assert call["temperature"] == 0
    prompt = call["messages"][1]["content"]
    for name in CATEGORIES:
        assert name in prompt, "the model is only offered categories that exist"


@pytest.mark.asyncio
async def test_a_fenced_json_reply_is_still_understood(fake_llm, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    fake_llm["reply"] = '```json\n{"category": "فنی و مهندسی"}\n```'
    assert await llm_classification.classify_with_llm("x", CATEGORIES) == "فنی و مهندسی"


@pytest.mark.asyncio
async def test_a_category_that_does_not_exist_is_refused(fake_llm, monkeypatch):
    """The model cannot invent a category and leave a dangling reference."""
    from app.config import settings

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    fake_llm["reply"] = json.dumps({"category": "Astrophysics"})
    assert await llm_classification.classify_with_llm("x", CATEGORIES) is None


@pytest.mark.asyncio
async def test_case_differences_resolve_to_the_real_spelling(fake_llm, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    fake_llm["reply"] = json.dumps({"category": "engineering"})
    assert await llm_classification.classify_with_llm("x", ["Engineering"]) == "Engineering"


@pytest.mark.asyncio
async def test_a_provider_error_falls_back_to_the_rules(fake_llm, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    fake_llm["error"] = RuntimeError("connection reset")
    assert await llm_classification.classify_with_llm("x", CATEGORIES) is None


@pytest.mark.asyncio
async def test_no_usable_model_config_falls_back(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)

    async def _no_config(**_kwargs):
        return {"api_key": None, "model_name": "x"}

    import app.news.generation

    monkeypatch.setattr(app.news.generation, "resolve_generation_config", _no_config)
    assert await llm_classification.classify_with_llm("x", CATEGORIES) is None


@pytest.mark.asyncio
async def test_never_raises_for_junk_input(fake_llm, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    for junk in ("", "not json at all", "{broken", "{}", '{"category": null}', "[]"):
        fake_llm["reply"] = junk
        assert await llm_classification.classify_with_llm("x", CATEGORIES) in (None, *CATEGORIES)


# --------------------------------------------------------------------------
# the worker integration
# --------------------------------------------------------------------------


async def _seed(session: AsyncSession) -> None:
    for name in CATEGORIES:
        session.add(Category(name=name, description=name))
    await session.commit()


@pytest.mark.asyncio
async def test_worker_stores_the_rule_decision_when_the_flag_is_off(
    session: AsyncSession, fake_llm, monkeypatch
):
    from app.config import settings
    from app.worker.worker import resolve_field

    monkeypatch.setattr(settings, "use_llm_for_categorization", False)
    await _seed(session)
    field, category = await resolve_field(session, "هوش مصنوعی")
    assert field is not None
    assert category is not None
    assert field.category_source == "rules"
    assert fake_llm["instance"] is None, "no provider call with the flag off"


@pytest.mark.asyncio
async def test_worker_uses_the_llm_answer_and_records_it(
    session: AsyncSession, fake_llm, monkeypatch
):
    from app.config import settings
    from app.worker.worker import resolve_field

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    await _seed(session)
    fake_llm["reply"] = json.dumps({"category": "فنی و مهندسی"})
    field, category = await resolve_field(session, "حوزه‌ای که قواعد نمی‌شناسند")
    assert category.name == "فنی و مهندسی"
    assert field.category_source == "llm"
    assert field.matched_keyword is None, (
        "there was no keyword match, so recording one would be a lie"
    )


@pytest.mark.asyncio
async def test_worker_falls_back_to_the_rules_when_the_llm_fails(
    session: AsyncSession, fake_llm, monkeypatch
):
    """The critical guarantee: an LLM outage never stops thesis processing."""
    from app.config import settings
    from app.worker.worker import resolve_field

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    await _seed(session)
    rules_name, _kw, _ = classify_with_reason(normalize_field("هوش مصنوعی"))
    # The rules' answer must EXIST, or the worker would (correctly) fall
    # through to `other` and the test would not be testing the fallback.
    if (await session.execute(
        select(Category).where(Category.name == rules_name)
    )).scalar_one_or_none() is None:
        session.add(Category(name=rules_name, description=rules_name))
        await session.commit()
    fake_llm["error"] = RuntimeError("provider down")
    field, category = await resolve_field(session, "هوش مصنوعی")
    assert category is not None, "the thesis still gets a category"
    assert field.category_source == "rules", "the row says which path answered"
    assert category.name == rules_name


@pytest.mark.asyncio
async def test_the_decision_is_stored_once_and_never_re_asked(
    session: AsyncSession, fake_llm, monkeypatch
):
    """Item 15: the first answer is kept, so the flag costs one call ever."""
    from app.config import settings
    from app.worker.worker import resolve_field

    monkeypatch.setattr(settings, "use_llm_for_categorization", True)
    await _seed(session)
    fake_llm["reply"] = json.dumps({"category": "فنی و مهندسی"})
    await resolve_field(session, "میدان تازه و ناشناخته")
    first_calls = len(fake_llm["instance"].completions.calls)
    assert first_calls == 1

    fake_llm["reply"] = json.dumps({"category": "علوم اجتماعی و اقتصاد"})
    field, category = await resolve_field(session, "میدان تازه و ناشناخته")
    assert len(fake_llm["instance"].completions.calls) == first_calls, "no second call"
    assert category.name == "فنی و مهندسی", "the stored decision stands"
    stored = (await session.execute(
        select(Field).where(Field.normalized_name == normalize_field("میدان تازه و ناشناخته"))
    )).scalar_one()
    assert stored.category_id == category.id
