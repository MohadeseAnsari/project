"""OPTIONAL LLM categorisation for a newly seen field (item 20).

The deterministic rules in :mod:`app.fields.classification` are the default
and stay the default: they are instant, free, auditable (they report which
keyword matched) and cannot fail. This module is the opt-in alternative, gated
on ``settings.use_llm_for_categorization``.

Design constraints, in order of importance:

1. **It can only ever add, never break.** Any failure — flag off, no active
   model, no usable key, a timeout, a reply that names no known category, or a
   category that was deleted — returns ``None`` and the caller falls back to
   the rules. An unreachable provider must not stop thesis processing.
2. **It cannot invent a category.** The model is asked to pick from the
   category names that actually exist, and the answer is verified against
   them. This keeps ``fields.category_id`` referentially valid.
3. **It is a cost, so it is bounded.** One short call, one question, no
   reasoning, a hard timeout, and ``temperature=0`` for reproducibility.

Whatever this returns is stored once on the field row and read from then on
(item 15), so the flag only ever affects the FIRST sighting of a field.
"""

import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You classify an academic field of study into exactly one category. "
    "Reply with ONLY a JSON object: {\"category\": \"<one of the given names>\"}. "
    "No explanation, no other text."
)


def _prompt(field_name: str, category_names: list[str]) -> str:
    options = "\n".join(f"- {name}" for name in category_names)
    return (
        f"Field of study: {field_name}\n\n"
        f"Available categories (choose exactly one):\n{options}\n\n"
        "Reply with the JSON object only."
    )


async def classify_with_llm(field_name: str, category_names: list[str]) -> str | None:
    """Ask the active model to place one field. ``None`` means "use the rules".

    Returns a category name that is guaranteed to be one of
    ``category_names`` (case-insensitively matched back to the exact spelling),
    or ``None``.

    Never raises: every failure path returns ``None`` so the caller always has
    the deterministic rules to fall back on.
    """
    if not settings.use_llm_for_categorization:
        return None
    if not category_names or not field_name:
        return None
    try:
        from openai import AsyncOpenAI

        from app.news.generation import resolve_generation_config
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("LLM categorisation unavailable: %s", exc)
        return None

    try:
        config = await resolve_generation_config()
    except Exception as exc:
        logger.info("LLM categorisation skipped, no usable model config: %s", exc)
        return None
    if not config or not config.get("api_key"):
        return None

    model = config.get("api_model") or config.get("model_name")
    if not model:
        return None

    try:
        client = AsyncOpenAI(
            api_key=config["api_key"],
            base_url=config.get("base_url") or None,
            timeout=settings.llm_classification_timeout_seconds,
        )
        response = await client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=80,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _prompt(field_name, category_names)},
            ],
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:
        # Timeouts, 4xx/5xx, refusals: all mean "fall back", never "fail".
        logger.warning("LLM categorisation failed for %r, falling back to rules: %s",
                       field_name, exc)
        return None

    chosen = _extract_category(content, category_names)
    if chosen is None:
        logger.info("LLM returned no known category for %r, falling back to rules", field_name)
    return chosen


def _extract_category(content: str, category_names: list[str]) -> str | None:
    """Pull a category name out of the reply, verified against the real names.

    Tolerates a bare name or a fenced/prefixed JSON object, and matches
    case-insensitively so the returned string is guaranteed to be the exact
    spelling that exists in the database.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    payload: str | None = None
    try:
        payload = json.loads(text).get("category")
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                payload = json.loads(text[start:end + 1]).get("category")
            except Exception:
                payload = None
    if not payload:
        payload = text
    payload = str(payload).strip().strip('"').strip()
    for name in category_names:
        if name.lower() == payload.lower():
            return name
    return None
