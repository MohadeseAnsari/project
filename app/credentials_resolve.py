"""One credential resolution order, shared by every caller (PART D3).

Order, and it is the same everywhere (worker, panel regenerate, connection
probe, machine API):

1. ``base_url`` — the row's own endpoint, else the global setting;
2. ``api_model`` — the provider's real model id, else the global setting,
   else the row's display name;
3. key — the row's decrypted ``api_key_encrypted``, else ``api_key_env``
   read from the environment, else the global key.

**A row that defines its own ``base_url`` with no usable key fails loudly**
(:class:`MissingModelKeyError`) instead of borrowing another provider's key.
That was the bug behind the observed HTTP 401: a row pasted with a key that
did not match its endpoint sent that key anyway.

Nothing here returns a key to a caller that could render it; the panel and the
API use :func:`describe_credentials`, which reports presence and a masked
hint only.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.config import settings
from app.credentials import CredentialError, decrypt, mask

logger = logging.getLogger(__name__)


class MissingModelKeyError(ValueError):
    """A model row points at its own endpoint but has no usable key.

    Raised (never swallowed) so the failure lands loudly in
    ``main.error_message`` and on the panel instead of sending another
    provider's key to a new endpoint. The message names the model, the
    endpoint, and what is missing — never a key value.
    """


@dataclass(frozen=True)
class ResolvedCredentials:
    """Everything a caller needs to make one provider call."""

    base_url: str
    api_model: str
    api_key: str
    key_source: str  # "row" | "env" | "global" — for display, never the key


def _row_key(row) -> tuple[str | None, str]:
    """Decrypt the row's own key; ``(key, source)``.

    A stored value that cannot be decrypted (rotated master key, tampered
    data) is reported as absent *and logged*, so the operator sees the real
    reason instead of silently falling back to the global key.
    """
    stored = getattr(row, "api_key_encrypted", None)
    if stored:
        try:
            key = decrypt(stored)
        except CredentialError as exc:
            logger.error(
                "Model %s has an unreadable stored key (%s); treating it as absent",
                getattr(row, "name", "?"), exc,
            )
            return None, "unreadable"
        if key:
            return key, "row"
    env_name = getattr(row, "api_key_env", None)
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value, "env"
        if getattr(row, "base_url", None):
            return None, "missing-env"
    return None, "global"


def resolve_model_credentials(row) -> ResolvedCredentials:
    """Resolve base_url / api_model / key for one model row.

    Raises:
        MissingModelKeyError: the row defines its own ``base_url`` but has no
            usable key of its own.
    """
    row_base = getattr(row, "base_url", None)
    key, source = _row_key(row)

    if row_base and not key:
        # The exact failure the owner hit: never send the global (another
        # provider's) key to an endpoint this row introduced.
        detail = (
            f"environment variable '{row.api_key_env}' is not set or empty"
            if getattr(row, "api_key_env", None)
            else "no key is configured for this row"
        )
        raise MissingModelKeyError(
            f"model '{getattr(row, 'name', '?')}' defines base_url '{row_base}' "
            f"but {detail}. Set the model's own key in the panel; the global key "
            f"is never sent to a new endpoint."
        )

    return ResolvedCredentials(
        base_url=row_base or settings.avalai_base_url,
        api_model=getattr(row, "api_model", None) or getattr(row, "name", None)
        or settings.llm_model,
        api_key=key or settings.avalai_api_key,
        key_source=source if key else "global",
    )


def describe_credentials(row) -> dict[str, object]:
    """Non-secret summary of a row's endpoint and key, for display.

    Reports where the key comes from, whether one is usable right now, and a
    masked hint. A row with its own endpoint and no key is reported as
    ``key_present=False`` so the panel can show the real reason instead of
    implying that the global key will be used.
    """
    row_base = getattr(row, "base_url", None)
    key, source = _row_key(row)
    return {
        "base_url": row_base or settings.avalai_base_url,
        "api_model": getattr(row, "api_model", None) or getattr(row, "name", None),
        "key_env": getattr(row, "api_key_env", None) or None,
        "key_source": source if key else ("global" if not row_base else "missing"),
        "key_present": bool(key) or not row_base,
        "key_hint": mask(key),
        "uses_global_key": not row_base,
        "needs_own_key": bool(row_base) and not bool(key),
    }