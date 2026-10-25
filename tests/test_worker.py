import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Main, Field, Model, Prompt
from app.schemas.news import CategoryMatchCompact, GeneratedNews
from app.worker.worker import recover_stale_jobs, claim_next_job, _upsert_field, process_job, fail_job

# NOTE (v2-combined): the old ``test_resolve_field_*`` tests are gone with the
# rule-based resolver itself — the field row is now only a dedup identity
# (see ``_upsert_field``), classification lives in ``main_category_matches``
# and is covered by ``tests/test_combined_classification.py``.


@pytest.mark.asyncio
async def test_recover_stale_jobs(session: AsyncSession):
    stale_time = datetime.utcnow() - timedelta(minutes=10)
    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
        claimed_at=stale_time,
        heartbeat_at=stale_time,
    )
    session.add(job)
    await session.commit()

    recovered = await recover_stale_jobs(session)
    assert recovered == 1

    await session.refresh(job)
    assert job.status == "ready"
    assert job.claimed_at is None


@pytest.mark.asyncio
async def test_claim_next_job(session: AsyncSession):
    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="ready",
    )
    session.add(job)
    await session.commit()

    claimed = await claim_next_job(session)
    assert claimed is not None
    assert claimed.status == "processing"
    assert claimed.claimed_at is not None


@pytest.mark.asyncio
async def test_upsert_field_inserts_only_the_dedup_identity(session: AsyncSession):
    """A field row is only a dedup identity now: no category or keyword."""
    field = await _upsert_field(session, "پزشکی")
    await session.commit()
    assert field is not None
    assert not hasattr(field, "category_id")
    assert not hasattr(field, "matched_keyword")
    assert field.source == "runtime"


@pytest.mark.asyncio
async def test_upsert_field_dedupes_normalized(session: AsyncSession):
    """Same normalized identity → same row (first sighting wins)."""
    first = await _upsert_field(session, "پزشکی")
    # Zero-width space must normalize to the same identity.
    second = await _upsert_field(session, "پزشکی\u200b")
    await session.commit()
    assert first is not None and second is not None
    assert first.id == second.id

    count = await session.execute(select(func.count()).select_from(Field))
    assert count.scalar_one() == 1


@pytest.mark.asyncio
async def test_process_job_success(session: AsyncSession):
    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    output = GeneratedNews(
        title="Generated Title",
        lead="Generated Lead",
        body="Generated Body",
        categories=[],
    )
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    await session.refresh(job)
    assert job.status == "review"
    assert job.output_json is not None
    assert "title" in job.output_json
    assert "lead" in job.output_json
    assert "body" in job.output_json


@pytest.mark.asyncio
async def test_fail_job(session: AsyncSession):
    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    await fail_job(session, job, "LLM API error")

    await session.refresh(job)
    assert job.status == "failed"
    assert job.error_message == "LLM API error"


@pytest.mark.asyncio
async def test_claim_next_job_fifo_order(session: AsyncSession):
    older = Main(
        username="test",
        title="Older",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="ready",
        created_at=datetime.utcnow() - timedelta(minutes=5),
    )
    newer = Main(
        username="test",
        title="Newer",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="ready",
        created_at=datetime.utcnow(),
    )
    session.add_all([older, newer])
    await session.commit()

    claimed = await claim_next_job(session)
    assert claimed is not None
    assert claimed.id == older.id


@pytest.mark.asyncio
async def test_process_job_failure_marks_failed(session: AsyncSession):
    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.side_effect = RuntimeError("LLM API error")
        with pytest.raises(RuntimeError):
            await process_job(session, job)

    await fail_job(session, job, "LLM API error")

    await session.refresh(job)
    assert job.status == "failed"
    assert job.error_message == "LLM API error"


@pytest.mark.asyncio
async def test_process_job_sets_model_prompt_and_heartbeat(session: AsyncSession):
    model = Model(name="gpt-5.6-luna", provider="openai", version="latest", is_active=True)
    prompt = Prompt(version="v1", system_prompt="test prompt", is_active=True)
    session.add_all([model, prompt])
    await session.commit()

    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    output = GeneratedNews(title="T", lead="L", body="B", categories=[])
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    await session.refresh(job)
    assert job.status == "review"
    assert job.model_id == model.id
    assert job.prompt_id == prompt.id
    assert job.heartbeat_at is not None


@pytest.mark.asyncio
async def test_process_job_stores_classification(session: AsyncSession):
    """The combined call's categories land in main_category_matches."""
    from app.db.models import Category, MainCategoryMatch

    session.add_all([
        Category(name="بهداشت", deprecated=False, description="health", short_code=24),
        Category(name="فناوری اطلاعات و ارتباطات", deprecated=False, description="ict", short_code=16),
        Category(name="other", deprecated=False, description="fallback", short_code=0),
    ])
    await session.commit()

    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    output = GeneratedNews(
        title="T", lead="L", body="B",
        categories=[
            CategoryMatchCompact(code=24, score=92, reason="پزشکی است"),
            CategoryMatchCompact(code=16, score=70, reason="ai"),
        ],
    )
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    await session.refresh(job)
    rows = (await session.execute(
        select(MainCategoryMatch).where(MainCategoryMatch.main_id == job.id)
    )).scalars().all()
    assert len(rows) == 2
    top = next(r for r in rows if r.rank == 1)
    assert top.score == 0.92
    assert job.category_id == top.category_id
    assert job.status == "review"


@pytest.mark.asyncio
async def test_process_job_no_qualifying_match_falls_back_to_other(session: AsyncSession):
    """Empty categories → exactly one 'other' row, no qualifying match."""
    from app.db.models import Category, MainCategoryMatch

    session.add(Category(name="other", deprecated=False, description="fallback", short_code=0))
    await session.commit()

    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    output = GeneratedNews(title="T", lead="L", body="B", categories=[])
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    await session.refresh(job)
    rows = (await session.execute(
        select(MainCategoryMatch).where(MainCategoryMatch.main_id == job.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].score == 0.0
    assert rows[0].rank == 1
    assert rows[0].reason == "no auto-match"
    other = (await session.execute(
        select(Category).where(Category.name == "other")
    )).scalar_one()
    assert job.category_id == other.id


@pytest.mark.asyncio
async def test_secret_not_sent_to_llm(session: AsyncSession):
    job = Main(
        username="test",
        title="Test",
        abstract="Test abstract",
        field="پزشکی",
        university="Test University",
        author="Author",
        supervisor="Supervisor",
        status="processing",
    )
    session.add(job)
    await session.commit()

    output = GeneratedNews(title="T", lead="L", body="B", categories=[])
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    thesis_data = mock_gen.call_args[0][0]
    assert "shared_Secret" not in thesis_data
    assert settings.shared_secret not in str(list(thesis_data.values()))
