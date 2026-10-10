"""LLM news generation via PydanticAI against AvalAI's OpenAI-compatible API.

The LLM is used ONLY for journalistic news generation (Assignment 2/3
boundary). Field/category classification never goes through this module.
"""

import logging
import os
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from openai import AsyncOpenAI

from app.config import settings
from app.news.prompts import SYSTEM_PROMPT
from app.schemas.news import GeneratedNews

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_agent: Agent | None = None


def resolve_model_name(override_name: str | None, active_name: str | None) -> str:
    """Resolve the model identifier: override > active > config fallback.

    The config fallback (``settings.llm_model``) keeps generation working when
    the database has no model rows (fresh installs, tests without seeds).
    """
    if override_name:
        return override_name
    if active_name:
        return active_name
    return settings.llm_model


def resolve_system_prompt(override_prompt: str | None, active_prompt: str | None) -> str:
    """Resolve the system prompt: override > active > packaged fallback."""
    if override_prompt:
        return override_prompt
    if active_prompt:
        return active_prompt
    return SYSTEM_PROMPT


def resolve_base_url(model_base_url: str | None) -> str:
    """Resolve the provider endpoint: per-model override else config."""
    return model_base_url or settings.avalai_base_url


class MissingModelKeyError(ValueError):
    """A model row points at its own endpoint but no usable key exists.

    Raised (never swallowed) so the failure lands loudly in
    ``main.error_message`` and on the panel instead of sending another
    provider's key to a new endpoint. The message names the model, the
    endpoint, and the missing variable — never a key value.
    """


def resolve_api_key(api_key_env: str | None, base_url: str | None = None) -> str:
    """Resolve the provider key for a model row, explicitly.

    - The row defines ``base_url``: only that endpoint's own key is usable
      (``api_key_env`` read from the environment). A missing variable name,
      an unset variable, or an empty value raises :class:`MissingModelKeyError`
      instead of silently substituting the global key.
    - The row defines no ``base_url``: the row shares the configured
      endpoint, so its ``api_key_env`` (when usable) or otherwise the global
      ``settings.avalai_api_key`` applies — the historical behavior.
    """
    usable = api_key_env and os.environ.get(api_key_env)
    if base_url:
        if usable:
            return usable
        if not api_key_env:
            raise MissingModelKeyError(
                f"model defines base_url '{base_url}' but has no api_key_env configured; "
                "set the variable name instead of falling back to the global key"
            )
        raise MissingModelKeyError(
            f"model defines base_url '{base_url}' but environment variable "
            f"'{api_key_env}' is not set or empty"
        )
    return usable or settings.avalai_api_key


def describe_model_credentials(model_row) -> dict[str, object]:
    """Describe a model row's resolved endpoint/key WITHOUT key values.

    Returns ``base_url`` (row or global fallback), ``key_env`` (the variable
    name, or ``None``), ``key_present`` (whether a usable key exists right
    now), and ``uses_global_key``. The panel renders source + present/missing
    badges from this; nothing here can leak a secret.
    """
    base_url = model_row.base_url or settings.avalai_base_url
    key_env = model_row.api_key_env or None
    if model_row.base_url:
        present = bool(key_env and os.environ.get(key_env))
    else:
        present = bool((key_env and os.environ.get(key_env)) or settings.avalai_api_key)
    return {
        "base_url": base_url,
        "key_env": key_env,
        "key_present": present,
        "uses_global_key": not bool(model_row.base_url),
    }


def resolve_generation_config(
    model_override, prompt_override, active_model, active_prompt
) -> dict[str, Any]:
    """Resolve one generation call: override > active > config fallback.

    This is the SINGLE shared resolution used by both the generation Worker
    (``app.worker.worker``) and the panel regenerate action (``app.panel``),
    so a record is generated with exactly the same model, prompt, endpoint,
    and key no matter which side triggers it. Row arguments are ``Model`` /
    ``Prompt`` ORM objects or ``None``; only names/texts are read out, so the
    caller keeps its own rows for audit FKs.
    """
    override_base = getattr(model_override, "base_url", None)
    active_base = getattr(active_model, "base_url", None)
    override_env = getattr(model_override, "api_key_env", None)
    active_env = getattr(active_model, "api_key_env", None)
    effective_base = override_base or active_base
    # The key always travels with the row that provides the endpoint: a
    # base_url without its own usable key fails loudly (MissingModelKeyError)
    # instead of borrowing another row's — or the global — key.
    effective_env = override_env if override_base else (active_env if active_base else (override_env or active_env))
    base_url = resolve_base_url(effective_base)
    # Key resolution may raise MissingModelKeyError: the caller (Worker fail
    # path, panel regenerate, connection probe) surfaces it loudly instead of
    # sending the wrong provider's key to this endpoint.
    return {
        "model_name": resolve_model_name(
            getattr(model_override, "name", None),
            getattr(active_model, "name", None),
        ),
        "system_prompt": resolve_system_prompt(
            getattr(prompt_override, "system_prompt", None),
            getattr(active_prompt, "system_prompt", None),
        ),
        "base_url": base_url,
        "api_key": resolve_api_key(effective_env, effective_base or None),
    }


async def probe_model_connection(
    model_name: str, base_url: str | None = None, api_key_env: str | None = None
) -> tuple[bool, str | None]:
    """Make a real minimal 1-token provider call; return ``(ok, error)``.

    Shared by the machine API (``POST /models/{id}/test-connection``) and the
    panel model test button, so both report the same result for the same row:
    the resolved key/base_url are exercised, and a missing key reports its
    real reason instead of raising. The key value is never included in the
    error text or logged anywhere.
    """
    try:
        effective_key = resolve_api_key(api_key_env, base_url)
    except MissingModelKeyError as e:
        return False, str(e)
    effective_base_url = base_url or settings.avalai_base_url
    if not effective_key:
        return False, "no API key configured"
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=effective_key, base_url=effective_base_url, timeout=15.0)
        # Minimal probe: 1 token, discarded. Proves auth + route + model name.
        await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1,
        )
        return True, None
    except Exception as e:
        # Never include the key; provider exception text is safe to surface.
        return False, str(e)[:500]


def _get_client() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI client, creating it on first use.

    The client is configured with a bounded timeout (see
    ``settings.llm_timeout_seconds``) so a hung provider cannot block the
    single sequential worker indefinitely.
    """
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.avalai_api_key,
            base_url=settings.avalai_base_url,
            timeout=settings.llm_timeout_seconds,
        )
    return _client


def _get_agent() -> Agent:
    """Return the shared PydanticAI agent, creating it on first use.

    The agent is bound to the ``GeneratedNews`` output type, so every run
    returns a structured ``{title, lead, body}`` result validated by Pydantic.
    """
    global _agent
    if _agent is None:
        model = OpenAIChatModel(
            settings.llm_model,
            provider=OpenAIProvider(openai_client=_get_client()),
        )
        _agent = Agent(model, output_type=GeneratedNews, system_prompt=SYSTEM_PROMPT)
    return _agent


def build_user_message(thesis_data: dict[str, Any]) -> str:
    """Build the exact user message sent to the LLM for a thesis.

    Single template shared by generation and the panel preview, so what the
    reviewer sees is byte-identical to what the model received. Thesis values
    are data, never instructions (see the system prompt); missing values
    render empty rather than failing the job.
    """
    return f"""Thesis metadata:
title: {thesis_data.get('title', '')}
abstract: {thesis_data.get('abstract', '')}
field: {thesis_data.get('field', '')}
university: {thesis_data.get('university', '')}
table_of_contents: {thesis_data.get('table_of_contents', '') or ''}
author: {thesis_data.get('author', '')}
supervisor: {thesis_data.get('supervisor', '')}"""


async def generate_news(
    thesis_data: dict[str, Any],
    model_name: str | None = None,
    system_prompt: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> GeneratedNews:
    """Generate a journalistic news item from thesis metadata.

    Args:
        thesis_data: Thesis metadata (title, abstract, field, university,
            table_of_contents, author, supervisor). This is treated strictly
            as data — the system prompt instructs the model to never follow
            instructions contained in it, and no secrets are included.
        model_name: Resolved model identifier (override > active > config).
            When None, the configured ``settings.llm_model`` is used.
        system_prompt: Resolved system prompt (override > active > packaged).
            When None, the packaged ``SYSTEM_PROMPT`` is used.
        base_url: Resolved provider endpoint (per-model else config).
        api_key: Resolved provider key (never logged).

    Returns:
        A structured ``GeneratedNews`` with Persian ``title``, ``lead`` and
        ``body``.

    Raises:
        Exception: Any provider/validation error propagates to the caller
            (the Worker marks the job as ``failed``).
    """
    effective_model = model_name or settings.llm_model
    effective_prompt = system_prompt or SYSTEM_PROMPT
    effective_base_url = base_url or settings.avalai_base_url
    # Note: api_key resolution stays here so every path uses the same fallback;
    # the key itself is never logged.
    effective_key = api_key if api_key is not None else settings.avalai_api_key

    if (
        effective_model == settings.llm_model
        and effective_prompt == SYSTEM_PROMPT
        and effective_base_url == settings.avalai_base_url
        and effective_key == settings.avalai_api_key
    ):
        agent = _get_agent()
    else:
        client = AsyncOpenAI(
            api_key=effective_key,
            base_url=effective_base_url,
            timeout=settings.llm_timeout_seconds,
        )
        model = OpenAIChatModel(
            effective_model,
            provider=OpenAIProvider(openai_client=client),
        )
        agent = Agent(model, output_type=GeneratedNews, system_prompt=effective_prompt)

    user_message = build_user_message(thesis_data)

    result = await agent.run(user_message)
    return result.output
