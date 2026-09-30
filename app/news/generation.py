"""LLM news generation via PydanticAI against AvalAI's OpenAI-compatible API.

The LLM is used ONLY for journalistic news generation (Assignment 2/3
boundary). Field/category classification never goes through this module.
"""

import logging
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


async def generate_news(thesis_data: dict[str, Any]) -> GeneratedNews:
    """Generate a journalistic news item from thesis metadata.

    Args:
        thesis_data: Thesis metadata (title, abstract, field, university,
            table_of_contents, author, supervisor). This is treated strictly
            as data — the system prompt instructs the model to never follow
            instructions contained in it, and no secrets are included.

    Returns:
        A structured ``GeneratedNews`` with Persian ``title``, ``lead`` and
        ``body``.

    Raises:
        Exception: Any provider/validation error propagates to the caller
            (the Worker marks the job as ``failed``).
    """
    agent = _get_agent()
    user_message = f"""Thesis metadata:
title: {thesis_data.get('title', '')}
abstract: {thesis_data.get('abstract', '')}
field: {thesis_data.get('field', '')}
university: {thesis_data.get('university', '')}
table_of_contents: {thesis_data.get('table_of_contents', '') or ''}
author: {thesis_data.get('author', '')}
supervisor: {thesis_data.get('supervisor', '')}"""

    result = await agent.run(user_message)
    return result.output
