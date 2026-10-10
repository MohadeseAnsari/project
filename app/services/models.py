"""Model domain service: CRUD, single-active switching, connection tests.

Single implementation shared by the machine API and the panel. Raises plain
``ValueError`` with stable messages; routes map them to HTTP statuses or
error pages. Password-style secrets never pass through here — only the
``api_key_env`` variable NAME, exactly as stored.
"""

from sqlalchemy import select, update

from app.db.models import Model
from app.news.generation import probe_model_connection


async def list_models(session) -> list[Model]:
    """All model rows in id order (panel table, submit selects)."""
    return list((await session.execute(select(Model).order_by(Model.id))).scalars().all())


async def get_model(session, model_id: int) -> Model | None:
    """Fetch one model row by id (both layers 404 the same way)."""
    return await session.get(Model, model_id)


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
) -> Model:
    """Insert a model row; with ``is_active`` it atomically deactivates the rest.

    Raises:
        ValueError: ``duplicate-name`` when the name is taken.
    """
    existing = await session.execute(select(Model).where(Model.name == name))
    if existing.scalar_one_or_none():
        raise ValueError("duplicate-name")
    row = Model(
        name=name, provider=provider, version=version, base_url=base_url,
        api_key_env=api_key_env, temperature=temperature,
        max_output_tokens=max_output_tokens, description=description,
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
    the field); absent keys are left alone. Callers decide the mapping: the
    machine API drops unset fields, the panel form maps blanks to ``None``.
    Unknown keys are ignored, never written.
    """
    allowed = {
        "provider", "version", "base_url", "api_key_env",
        "temperature", "max_output_tokens", "description",
    }
    for field_name, value in updates.items():
        if field_name in allowed:
            setattr(row, field_name, value)
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


async def test_model_connection(row: Model) -> tuple[bool, str | None]:
    """Probe one model row with the shared 1-token helper (key never leaks)."""
    return await probe_model_connection(row.name, row.base_url, row.api_key_env)
