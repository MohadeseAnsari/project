"""Shared thesis intake: one flow for the machine API and the human panel.

``POST /submit`` (shared-secret auth) and the panel submit form (cookie auth)
differ only in authentication and payload shape. Both must insert exactly the
same ``main`` row with status ``ready`` so the generation Worker picks it up
on its next poll with no extra glue. This module holds that single flow;
both callers import it instead of duplicating the validation and the insert.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Main, Model, Prompt


async def create_thesis_record(
    session: AsyncSession,
    *,
    username: str,
    title: str,
    abstract: str,
    field: str,
    university: str,
    table_of_contents: str | None,
    author: str,
    supervisor: str,
    model_id: int | None = None,
    prompt_id: int | None = None,
) -> Main:
    """Insert a ``ready`` thesis job with validated per-record overrides.

    Raises:
        ValueError: ``model_id``/``prompt_id`` names a row that does not exist.
            Callers map this to their own 422/error surface (API JSON vs panel
            page) without changing the check itself.
    """
    if model_id is not None and await session.get(Model, model_id) is None:
        raise ValueError("model_id does not exist")
    if prompt_id is not None and await session.get(Prompt, prompt_id) is None:
        raise ValueError("prompt_id does not exist")
    record = Main(
        username=username,
        title=title,
        abstract=abstract,
        field=field,
        university=university,
        table_of_contents=table_of_contents,
        author=author,
        supervisor=supervisor,
        status="ready",
        model_id=model_id,
        prompt_id=prompt_id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record
