import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Main, Field, Category, Model, Prompt
from app.worker.worker import recover_stale_jobs, claim_next_job, resolve_field, process_job, fail_job


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
async def test_claim_returns_none_when_no_ready_jobs(session: AsyncSession):
    claimed = await claim_next_job(session)
    assert claimed is None


@pytest.mark.asyncio
async def test_resolve_field_known(session: AsyncSession):
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()

    field = Field(name="پزشکی", normalized_name="پزشکی", category_id=cat.id)
    session.add(field)
    await session.commit()

    result_field, result_cat = await resolve_field(session, "پزشکی")
    assert result_field is not None
    assert result_field.name == "پزشکی"
    assert result_cat is not None
    assert result_cat.name == "پزشکی و علوم سلامت"


@pytest.mark.asyncio
async def test_resolve_field_unknown(session: AsyncSession):
    other_cat = Category(name="other", description="Other")
    session.add(other_cat)
    await session.commit()

    result_field, result_cat = await resolve_field(session, "unknown field xyz")
    assert result_field is not None
    assert result_field.source == "runtime"
    assert result_cat is not None
    assert result_cat.name == "other"


@pytest.mark.asyncio
async def test_process_job_success(session: AsyncSession):
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()

    field = Field(name="پزشکی", normalized_name="پزشکی", category_id=cat.id)
    session.add(field)
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

    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = AsyncMock(
            title="Generated Title",
            lead="Generated Lead",
            body="Generated Body",
        )
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
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()

    field = Field(name="پزشکی", normalized_name="پزشکی", category_id=cat.id)
    session.add(field)
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
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()

    field = Field(name="پزشکی", normalized_name="پزشکی", category_id=cat.id)
    session.add(field)
    await session.commit()

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

    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = AsyncMock(title="T", lead="L", body="B")
        await process_job(session, job)

    await session.refresh(job)
    assert job.status == "review"
    assert job.model_id == model.id
    assert job.prompt_id == prompt.id
    assert job.heartbeat_at is not None


@pytest.mark.asyncio
async def test_secret_not_sent_to_llm(session: AsyncSession):
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()

    field = Field(name="پزشکی", normalized_name="پزشکی", category_id=cat.id)
    session.add(field)
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

    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = AsyncMock(title="T", lead="L", body="B")
        await process_job(session, job)

    thesis_data = mock_gen.call_args[0][0]
    assert "shared_Secret" not in thesis_data
    assert settings.shared_secret not in str(list(thesis_data.values()))


@pytest.mark.asyncio
async def test_resolve_field_specific_not_swallowed(session: AsyncSession):
    art = Category(name="هنر و معماری", description="Art and architecture")
    session.add(art)
    await session.commit()

    specific, cat = await resolve_field(session, "معماری داخلی")
    assert specific is not None
    assert specific.name == "معماری داخلی"
    assert specific.normalized_name == "معماری داخلی"
    assert specific.source == "runtime"
    assert cat.name == "هنر و معماری"

    broad, cat2 = await resolve_field(session, "معماری")
    assert broad is not None
    assert broad.name == "معماری"
    assert broad.id != specific.id
    assert cat2.name == "هنر و معماری"


@pytest.mark.asyncio
async def test_resolve_field_duplicate_normalized(session: AsyncSession):
    med = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(med)
    await session.commit()

    first, _ = await resolve_field(session, "پزشکی")
    second, _ = await resolve_field(session, "پزشکی\u200b")
    assert first.id == second.id

    count = await session.execute(select(func.count()).select_from(Field))
    assert count.scalar_one() == 1
