"""Persistent, sequential Worker that processes thesis jobs from PostgreSQL.

PostgreSQL acts as both the persistent store and the job queue (Assignment
2/3): the API creates ``ready`` records, and this Worker claims them one at a
time in creation order (FIFO), runs preprocessing/classification and LLM
generation, and stores the result.

Failure/recovery model (Assignment 4):
- A job is claimed with ``FOR UPDATE SKIP LOCKED`` and moves to ``processing``
  with ``claimed_at``/``heartbeat_at`` timestamps.
- While the LLM call runs, a heartbeat task refreshes ``heartbeat_at``.
- If the Worker dies (crash, container kill, host restart), the job stays in
  ``processing`` with a stale heartbeat. On the next loop iteration
  ``recover_stale_jobs`` resets such jobs to ``ready`` so they are retried —
  this covers both a crash before the LLM call and a crash after the LLM
  response but before the DB commit (at-least-once processing; a duplicate
  LLM call is the accepted cost and is documented as an engineering decision).
- There is no invented retry counter: a job that fails (e.g. LLM error) is
  marked ``failed`` with the error message and never retried automatically.
"""

import asyncio
import logging
import signal
from datetime import datetime, timedelta

from sqlalchemy import select, text, update

from app.config import settings
from app.db.connection import async_session
from app.db.models import Category, Field, Main, Model, Prompt
from app.fields.classification import classify_field
from app.fields.normalization import normalize_field
from app.news.generation import generate_news

logger = logging.getLogger(__name__)


async def recover_stale_jobs(session) -> int:
    """Reset abandoned ``processing`` jobs back to ``ready``.

    A job is considered abandoned when its ``heartbeat_at`` is older than
    ``settings.worker_lease_minutes``, which means the Worker that claimed it
    died (crash, container kill, host restart) before committing a result.

    Returns:
        Number of jobs recovered.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=settings.worker_lease_minutes)
    result = await session.execute(
        update(Main)
        .where(Main.status == "processing")
        .where(Main.heartbeat_at < cutoff)
        .values(status="ready", claimed_at=None, heartbeat_at=None)
    )
    await session.commit()
    if result.rowcount:
        logger.warning("Recovered %d stale processing jobs", result.rowcount)
    return result.rowcount


async def claim_next_job(session) -> Main | None:
    """Atomically claim the oldest ``ready`` job (FIFO by ``created_at``).

    Uses ``FOR UPDATE SKIP LOCKED`` so the claim is safe even if additional
    Workers are ever introduced; with the single-Worker design it simply
    guarantees the row is locked for the duration of the transaction.

    Returns:
        The claimed job with status ``processing`` and fresh
        ``claimed_at``/``heartbeat_at``, or ``None`` when the queue is empty.
    """
    result = await session.execute(
        select(Main)
        .where(Main.status == "ready")
        .order_by(Main.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = result.scalar_one_or_none()
    if job:
        job.status = "processing"
        job.claimed_at = datetime.utcnow()
        job.heartbeat_at = datetime.utcnow()
        await session.commit()
        logger.info("Claimed job id=%s field=%s", job.id, job.field)
    return job


async def resolve_field(session, raw_field: str) -> tuple[Field | None, Category | None]:
    """Resolve a raw thesis field to a stored ``Field`` and its ``Category``.

    First tries an exact match on the normalized name (fast path, no write).
    For unknown fields, deterministically classifies the normalized name with
    rule-based keywords (never the LLM) and inserts a new ``Field`` with
    ``source='runtime'`` and a stable normalized identity, so repeated
    submissions of the same unknown field reuse one record.

    Returns:
        ``(field, category)``; ``field`` is ``None`` only if no category could
        be determined (the ``other`` category always exists, so this is
        defensive).
    """
    norm = normalize_field(raw_field)
    result = await session.execute(
        select(Field).where(Field.normalized_name == norm)
    )
    field = result.scalar_one_or_none()
    if field:
        cat_result = await session.execute(
            select(Category).where(Category.id == field.category_id)
        )
        return field, cat_result.scalar_one()

    cat_name = classify_field(norm)
    cat_result = await session.execute(
        select(Category).where(Category.name == cat_name)
    )
    category = cat_result.scalar_one_or_none()
    if not category:
        cat_result = await session.execute(
            select(Category).where(Category.name == "other")
        )
        category = cat_result.scalar_one()

    field = Field(
        name=raw_field,
        normalized_name=norm,
        category_id=category.id,
        source="runtime",
    )
    session.add(field)
    await session.commit()
    logger.info("Created new field id=%s name=%s category=%s", field.id, norm, cat_name)
    return field, category


async def get_active_model(session) -> Model | None:
    """Return the active LLM model configuration (for auditability)."""
    result = await session.execute(
        select(Model).where(Model.is_active == True).limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_prompt(session) -> Prompt | None:
    """Return the active prompt version (for auditability)."""
    result = await session.execute(
        select(Prompt).where(Prompt.is_active == True).limit(1)
    )
    return result.scalar_one_or_none()


async def heartbeat_loop(job_id: int, stop_event: asyncio.Event) -> None:
    """Refresh the job's ``heartbeat_at`` until ``stop_event`` is set.

    Uses its OWN async session (separate from the processing session) because
    a SQLAlchemy AsyncSession must not be shared concurrently between tasks.
    Runs while the LLM call is in flight so that long generations are not
    mistaken for a dead Worker by the stale-job recovery mechanism.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.worker_heartbeat_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            async with async_session() as hb_session:
                await hb_session.execute(
                    update(Main).where(Main.id == job_id).values(heartbeat_at=datetime.utcnow())
                )
                await hb_session.commit()
        except Exception as e:
            logger.warning("Heartbeat update failed: %s", e)


async def process_job(session, job: Main) -> None:
    """Process one claimed job: classify, generate news, store the result.

    Commits field/category/model/prompt links BEFORE the LLM call (so the
    classification is durable even if generation fails), then commits the
    generated output and moves the job to ``review``.

    Model/prompt resolution is override > active > config fallback: a
    per-record ``model_id``/``prompt_id`` from ``POST /submit`` is kept and
    used for generation; otherwise the active row is linked; otherwise the
    configured model and packaged prompt are used.

    Raises:
        Exception: Any failure (e.g. LLM error) propagates to the caller,
            which marks the job as ``failed``.
    """
    from app.news.generation import (
        resolve_api_key,
        resolve_base_url,
        resolve_model_name,
        resolve_system_prompt,
    )

    logger.info("Processing job id=%s", job.id)

    field_obj, category_obj = await resolve_field(session, job.field)
    if field_obj:
        job.field_id = field_obj.id
    if category_obj:
        job.category_id = category_obj.id

    active_model = await get_active_model(session)
    active_prompt = await get_active_prompt(session)
    # Keep per-record overrides from /submit; only fill gaps from active rows.
    orig_model_id = job.model_id
    orig_prompt_id = job.prompt_id
    if job.model_id is None and active_model:
        job.model_id = active_model.id
    if job.prompt_id is None and active_prompt:
        job.prompt_id = active_prompt.id

    override_model = await session.get(Model, orig_model_id) if orig_model_id else None
    override_prompt = await session.get(Prompt, orig_prompt_id) if orig_prompt_id else None
    model_name = resolve_model_name(
        override_model.name if override_model else None,
        active_model.name if active_model else None,
    )
    prompt_text = resolve_system_prompt(
        override_prompt.system_prompt if override_prompt else None,
        active_prompt.system_prompt if active_prompt else None,
    )
    base_url = resolve_base_url(
        (override_model.base_url if override_model else None)
        or (active_model.base_url if active_model else None)
    )
    api_key = resolve_api_key(
        (override_model.api_key_env if override_model else None)
        or (active_model.api_key_env if active_model else None)
    )

    thesis_data = {
        "title": job.title,
        "abstract": job.abstract,
        "field": job.field,
        "university": job.university,
        "table_of_contents": job.table_of_contents,
        "author": job.author,
        "supervisor": job.supervisor,
    }

    job.heartbeat_at = datetime.utcnow()
    await session.commit()

    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat_loop(job.id, stop_event))
    try:
        output = await generate_news(
            thesis_data,
            model_name=model_name,
            system_prompt=prompt_text,
            base_url=base_url,
            api_key=api_key,
        )
    finally:
        stop_event.set()
        await heartbeat_task

    job.output_json = {"title": output.title, "lead": output.lead, "body": output.body}
    job.status = "review"
    job.updated_at = datetime.utcnow()
    await session.commit()
    logger.info("Completed job id=%s", job.id)


async def fail_job(session, job: Main, error: str) -> None:
    """Mark a job as ``failed`` with a truncated error message.

    The error message is truncated to 2000 characters to fit the column and
    never contains secrets (only exception text from the LLM/provider).
    """
    job.status = "failed"
    job.error_message = error[:2000]
    job.updated_at = datetime.utcnow()
    await session.commit()
    logger.error("Failed job id=%s error=%s", job.id, error)


async def run_worker() -> None:
    """Run the persistent Worker loop until a shutdown signal is received.

    Each iteration: recover stale jobs, claim the oldest ready job, process
    it, or wait one poll interval when the queue is empty. The Worker stays
    alive across individual job failures and empty-queue polls; only
    SIGTERM/SIGINT stop it (graceful shutdown after the current job).
    """
    logger.info("Worker started")
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    while not stop_event.is_set():
        try:
            async with async_session() as session:
                await recover_stale_jobs(session)
                job = await claim_next_job(session)
                if job is None:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=settings.worker_poll_interval_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                try:
                    await process_job(session, job)
                except Exception as e:
                    await fail_job(session, job, str(e))
        except Exception as e:
            logger.error("Worker loop error: %s", e)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.worker_poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    logger.info("Worker stopped")


def main() -> None:
    """Console entry point: configure logging and run the Worker loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
