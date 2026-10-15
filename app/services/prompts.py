"""Prompt domain service: immutable versions and single-active switching.

Single implementation shared by the machine API and the panel. Versions are
never edited in place: a new version is always a new row, and activation
atomically retires the previous one. Raises plain ``ValueError`` with
stable messages; routes map them to HTTP statuses or error pages.
"""

from sqlalchemy import select, update

from app.db.models import Prompt


async def list_prompts(session) -> list[Prompt]:
    """All prompt versions in id order (editor list, submit selects)."""
    return list((await session.execute(select(Prompt).order_by(Prompt.id))).scalars().all())


async def get_prompt(session, prompt_id: int) -> Prompt | None:
    """Fetch one prompt version by id (both layers 404 the same way)."""
    return await session.get(Prompt, prompt_id)


async def active_prompt(session) -> Prompt | None:
    """The currently active prompt version, if any."""
    return (
        await session.execute(select(Prompt).where(Prompt.is_active.is_(True)).limit(1))
    ).scalar_one_or_none()


async def create_prompt(
    session, *, version: str, system_prompt: str, is_active: bool = False
) -> Prompt:
    """Insert a new prompt version; with ``is_active`` it retires the rest.

    Raises:
        ValueError: ``duplicate-version`` when the version exists.
    """
    existing = await session.execute(select(Prompt).where(Prompt.version == version))
    if existing.scalar_one_or_none():
        raise ValueError("duplicate-version")
    row = Prompt(version=version, system_prompt=system_prompt, is_active=False)
    session.add(row)
    await session.flush()
    if is_active:
        await session.execute(update(Prompt).where(Prompt.id != row.id).values(is_active=False))
        row.is_active = True
    await session.commit()
    await session.refresh(row)
    return row


async def activate_prompt(session, row: Prompt) -> Prompt:
    """Activate exactly this version (previous stays as history)."""
    await session.execute(update(Prompt).where(Prompt.id != row.id).values(is_active=False))
    row.is_active = True
    await session.commit()
    await session.refresh(row)
    return row
