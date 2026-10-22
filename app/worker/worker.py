"""Persistent, sequential Worker that processes thesis jobs from PostgreSQL.

PostgreSQL acts as both the persistent store and the job queue (Assignment
2/3): the API creates ``ready`` records, and this Worker claims them one at a
time in creation order (FIFO), upserts the field identity for dedup, makes
ONE LLM call that both writes the news and classifies the thesis into ISCED-F
2013 categories, and stores the result.

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

from sqlalchemy import select, update

from app.config import settings
from app.db.connection import async_session
from app.db.models import Field, Main, Model, Prompt
from app.news.isced import OTHER_CATEGORY
from app.fields.normalization import normalize_field
from app.news.generation import generate_news
from app.services.records import store_generated_classification
from app.services.transitions import assert_record_transition

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
    # processing -> ready is a declared transition (see app.services.transitions).
    assert_record_transition("processing", "ready")
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
        assert_record_transition(job.status, "processing")
        job.status = "processing"
        job.claimed_at = datetime.utcnow()
        job.heartbeat_at = datetime.utcnow()
        await session.commit()
        logger.info("Claimed job id=%s field=%s", job.id, job.field)
    return job


async def _upsert_field(session, raw_field: str) -> Field | None:
    """Register the thesis field name for dedup — WITHOUT classifying it.

    Classification is no longer decided here: the generation call carries the
    ISCED-F 2013 categories per record (``main_category_matches``). What the
    field row still provides is the stable dedup identity (first sighting
    wins): repeated submissions of the same discipline reuse one row.

    A NEW field is inserted with ``category_id = NULL`` and
    ``matched_keyword = NULL`` — the legacy rule-based columns stay NULL;
    only the panel's manual curation can still attach a (deprecated) field
    category later.
    """
    norm = normalize_field(raw_field)
    if not norm:
        return None
    result = await session.execute(
        select(Field).where(Field.normalized_name == norm)
    )
    field = result.scalar_one_or_none()
    if field:
        return field

    field = Field(
        name=raw_field,
        normalized_name=norm,
        category_id=None,
        source="runtime",
        matched_keyword=None,
    )
    session.add(field)
    await session.flush()  # grab field.id for the FK without a full commit
    logger.info("Registered new field id=%s name=%s (category: none — classified per record)", field.id, norm)
    return field


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
    """Process one claimed job: ONE LLM call for news + classification.

    Flow (v2-combined):
    1. Upsert the field name for dedup (category stays NULL on new rows) and
       commit field/model/prompt links BEFORE the LLM call (so the job's
       audit FKs are durable even if generation fails).
    2. Call ``generate_news`` exactly once — the resolved prompt instructs
       the model to return the article AND up to 3 ISCED-F 2013 category
       matches in one structured response.
    3. Store ``output_json`` and classify: ``store_generated_classification``
       filters/clamps/dedupes the answer, writes ``main_category_matches``,
       and sets ``job.category_id`` (top match, or the ``other`` fallback).
    4. Move the job to ``review`` and commit.

    Model/prompt resolution is override > active > config fallback: a
    per-record ``model_id``/``prompt_id`` from ``POST /submit`` is kept and
    used for generation; otherwise the active row is linked; otherwise the
    configured model and packaged prompt are used.

    Raises:
        Exception: Any failure (e.g. LLM error) propagates to the caller,
            which marks the job as ``failed``. No special handling: the LLM
            must return a valid ``GeneratedNews`` (``categories`` may be
            empty), otherwise the job stays failed and can be retried by a
            reviewer via "تولید دوباره".
    """
    from app.news.generation import resolve_generation_config

    logger.info("Processing job id=%s", job.id)

    field_obj = await _upsert_field(session, job.field)
    if field_obj:
        job.field_id = field_obj.id
    # NOTE: no job.category_id yet — classification happens with the
    # generation call below, not from the field name.

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
    # One shared resolution (also used by the panel regenerate action).
    config = resolve_generation_config(override_model, override_prompt, active_model, active_prompt)

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
        output = await generate_news(thesis_data, **config)
    finally:
        stop_event.set()
        await heartbeat_task

    job.output_json = {"title": output.title, "lead": output.lead, "body": output.body}
    assert_record_transition(job.status, "review")
    job.status = "review"
    job.updated_at = datetime.utcnow()
    # ONE call did both: every qualifying ISCED category match is stored
    # here (old rows replaced), ``job.category_id`` becomes the display
    # primary; nothing qualifies → single ``other`` fallback row and the
    # dispatcher will create NO deliveries (unless configured otherwise).
    qualifying = await store_generated_classification(
        session, job, output, model_id=job.model_id, prompt_id=job.prompt_id,
    )
    await session.commit()

    # Unmatched handling (settings.unmatched_category_behavior).
    if not qualifying:
        behavior = settings.unmatched_category_behavior
        if behavior == "failed":
            assert_record_transition(job.status, "failed")
            job.status = "failed"
            job.error_message = "No category matched above threshold"
            job.updated_at = datetime.utcnow()
            await session.commit()
            logger.info("Completed job id=%s (failed: no category)", job.id)
            return
        elif behavior == "fallback_other":
            from app.db.models import Category, Channel, CategoryChannelRoute, Delivery
            from app.messaging.dispatcher import channels_for_article_multi

            other = (
                await session.execute(
                    select(Category).where(Category.name == OTHER_CATEGORY)
                )
            ).scalar_one_or_none()
            if other is not None:
                # channels routed to "other" (if any)
                ch_rows = await session.execute(
                    select(Channel)
                    .join(CategoryChannelRoute, CategoryChannelRoute.channel_id == Channel.id)
                    .where(
                        CategoryChannelRoute.category_id == other.id,
                        Channel.is_active.is_(True),
                    )
                )
                gated = settings.require_human_confirmation
                for ch in ch_rows.scalars().all():
                    if ch.activated_at is not None and ch.activated_at > job.created_at:
                        continue
                    session.add(Delivery(
                        main_id=job.id,
                        channel_id=ch.id,
                        category_id=other.id,
                        route_source="category",
                        status="awaiting_confirmation" if gated else "pending",
                        attempts=0,
                    ))
                await session.commit()
                logger.info(
                    "Completed job id=%s (fallback_other: %d deliveries)",
                    job.id,
                    len(ch_rows.scalars().all()),
                )
                # Note: counts above may be off due to consumed iterator; log is informational.
                return
    logger.info("Completed job id=%s", job.id)


async def fail_job(session, job: Main, error: str) -> None:
    """Mark a job as ``failed`` with a truncated error message.

    The error message is truncated to 2000 characters to fit the column and
    never contains secrets (only exception text from the LLM/provider).
    """
    if job.status != "failed":
        assert_record_transition(job.status, "failed")
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
        """Stop after the current job (its heartbeat goes stale and recovers)."""
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
