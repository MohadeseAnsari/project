"""Model domain service: CRUD, single-active switching, credentials, tests.

Single implementation shared by the machine API and the panel. Raises plain
``ValueError`` with stable messages; routes map them to HTTP statuses or
error pages.

Credentials: ``set_credential`` encrypts a key into ``api_key_encrypted``
before it reaches the row, and nothing here ever returns a decrypted key —
``describe`` reports presence plus a masked hint only (PART D2). Connection
tests write ``last_tested_at`` / ``last_test_ok`` / ``last_test_error`` /
``last_test_latency_ms`` on every probe, so the panel's status column is real
data rather than a claim (PART D4).
"""

import os
from datetime import datetime

from sqlalchemy import select, update

from app.db.models import Model
from app.credentials import encrypt
from app.credentials_resolve import MissingModelKeyError, resolve_model_credentials
from app.news.generation import describe_model_credentials, probe_model_connection


async def list_models(session) -> list[Model]:
    """All model rows in id order (panel table, submit selects)."""
    return list((await session.execute(select(Model).order_by(Model.id))).scalars().all())


async def get_model(session, model_id: int) -> Model | None:
    """Fetch one model row by id (both layers 404 the same way)."""
    return await session.get(Model, model_id)


async def usable_model_count(session, *, exclude_id: int | None = None) -> int:
    """How many models can actually generate (have a usable credential).

    Used by the deactivate/delete guards: the panel must never leave zero
    usable models, which would silently stop every generation.
    """
    rows = (await session.execute(select(Model))).scalars().all()
    count = 0
    for row in rows:
        if exclude_id is not None and row.id == exclude_id:
            continue
        try:
            resolve_model_credentials(row)
        except MissingModelKeyError:
            continue
        count += 1
    return count


async def create_model(
    session,
    *,
    name: str,
    provider: str,
    version: str,
    base_url: str | None = None,
    api_key_env: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    description: str | None = None,
    is_active: bool = False,
    api_model: str | None = None,
    api_key: str | None = None,
) -> Model:
    """Insert a model row; with ``is_active`` it atomically deactivates the rest.

    ``api_key`` is encrypted before storage and never returned.

    Raises:
        ValueError: ``duplicate-name`` when the name is taken,
            ``bad-credential`` when a row with its own base_url has no key.
    """
    existing = await session.execute(select(Model).where(Model.name == name))
    if existing.scalar_one_or_none():
        raise ValueError("duplicate-name")
    encrypted = encrypt(api_key)
    if base_url and not (encrypted or (api_key_env and os.environ.get(api_key_env))):
        raise ValueError("bad-credential")
    row = Model(
        name=name, provider=provider, version=version, base_url=base_url,
        api_key_env=api_key_env, temperature=temperature,
        max_output_tokens=max_output_tokens, description=description,
        api_model=api_model or None, api_key_encrypted=encrypted,
        is_active=False,
    )
    session.add(row)
    await session.flush()
    if is_active:
        await session.execute(update(Model).where(Model.id != row.id).values(is_active=False))
        row.is_active = True
    await session.commit()
    await session.refresh(row)
    return row


async def update_model(session, row: Model, updates: dict) -> Model:
    """Update a model row's connection fields (activation only via activate).

    Every key present in ``updates`` is written, including ``None`` (clear
    the field); absent keys are left alone. ``api_key`` is special: it is
    encrypted, and an empty string KEEPS the stored key (clearing is the
    explicit ``clear_api_key`` action). Unknown keys are ignored.
    """
    allowed = {
        "provider", "version", "base_url", "api_key_env", "api_model",
        "temperature", "max_output_tokens", "description",
    }
    api_key = updates.pop("api_key", None)
    if api_key:
        # Empty submit keeps whatever is stored; only a value replaces it.
        row.api_key_encrypted = encrypt(api_key)
    for field_name, value in updates.items():
        if field_name in allowed:
            setattr(row, field_name, value)
    await session.commit()
    await session.refresh(row)
    return row


async def clear_api_key(session, row: Model) -> Model:
    """Explicitly remove a stored key (the panel's "پاک کردن کلید" action)."""
    row.api_key_encrypted = None
    await session.commit()
    await session.refresh(row)
    return row


async def activate_model(session, row: Model) -> Model:
    """Activate exactly this row (all others off, atomically)."""
    await session.execute(update(Model).where(Model.id != row.id).values(is_active=False))
    row.is_active = True
    await session.commit()
    await session.refresh(row)
    return row


async def deactivate_model(session, row: Model) -> Model:
    """Deactivate a model, refusing to leave zero usable models.

    Raises:
        ValueError: ``last-usable-model`` when this is the only model that
            could actually generate.
    """
    if row.is_active and await usable_model_count(session, exclude_id=row.id) == 0:
        raise ValueError("last-usable-model")
    row.is_active = False
    await session.commit()
    await session.refresh(row)
    return row


async def delete_model(session, row: Model) -> None:
    """Delete an inactive model, refusing to leave zero usable models.

    The active model cannot be deleted (deactivate first) — it is the one the
    Worker is using right now.

    Raises:
        ValueError: ``model-active`` when the row is active,
            ``last-usable-model`` when it is the last usable one,
            ``model-in-use`` when records already reference it.
    """
    if row.is_active:
        raise ValueError("model-active")
    if await usable_model_count(session, exclude_id=row.id) == 0:
        raise ValueError("last-usable-model")
    from app.db.models import Main

    in_use = (
        await session.execute(select(Main.id).where(Main.model_id == row.id).limit(1))
    ).scalar_one_or_none()
    if in_use is not None:
        raise ValueError("model-in-use")
    await session.delete(row)
    await session.commit()


async def record_test_result(
    session, row: Model, *, ok: bool, error: str | None, latency_ms: int | None = None
) -> Model:
    """Persist the outcome of a connection probe on the row (PART D4).

    ``error`` is truncated provider text (endpoint + HTTP status); it must
    never contain key material, and the service does not add any.
    """
    row.last_tested_at = datetime.utcnow()
    row.last_test_ok = ok
    row.last_test_error = (error or None)
    row.last_test_latency_ms = latency_ms
    await session.commit()
    await session.refresh(row)
    return row


async def test_model_connection(row: Model) -> tuple[bool, str | None]:
    """Probe one model row with the shared 1-token helper (key never leaks).

    Uses the row's resolved credential (stored key, env variable, or — only
    when the row has no endpoint of its own — the global key).
    """
    try:
        resolved = resolve_model_credentials(row)
    except MissingModelKeyError as exc:
        return False, str(exc)
    return await probe_model_connection(
        resolved.api_model, resolved.base_url, None, api_key=resolved.api_key
    )


def describe(row: Model) -> dict[str, object]:
    """Non-secret credential summary for the panel/API (masked, never a key)."""
    return describe_model_credentials(row)


def test_status(row: Model) -> str:
    """Panel status column value: never tested / ok / failed with a reason."""
    if row.last_tested_at is None:
        return "never"
    if row.last_test_ok:
        return "ok"
    return "failed"


async def ensure_usable_model(session, *, candidate: Model | None = None) -> Model | None:
    """The model the panel should default to (active, else first usable)."""
    if candidate is not None:
        return candidate
    rows = await list_models(session)
    active = next((m for m in rows if m.is_active), None)
    if active is not None:
        return active
    return rows[0] if rows else None