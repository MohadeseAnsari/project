"""End-to-end integration test: API submission -> Worker processing -> review.

Runs against the real PostgreSQL database and the real application stack.
Two execution paths are possible and both are valid:

1. The test claims the submitted job itself and processes it with a mocked
   LLM (fast, deterministic).
2. The live Worker container claims the job first (it polls every second)
   and processes it with the real LLM. The test then observes the record
   reaching ``review`` through the API.

Either way the full product path (API -> PostgreSQL -> Worker -> result) is
exercised. Skipped automatically when PostgreSQL is not available.
"""

import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db.connection import async_session
from app.main import app
from app.worker.worker import claim_next_job, process_job


@pytest_asyncio.fixture
async def postgres_available():
    """Skip the test when PostgreSQL is not reachable.

    Runs inside the test's event loop so the shared engine's connection pool
    is not bound to a different (closed) loop.
    """
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL is not available: {e}")
    yield


async def _wait_for_terminal_status(record_id: str, attempts: int = 30) -> str:
    """Poll the record endpoint until the job reaches a terminal status."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(attempts):
            response = await client.get(
                f"/records/{record_id}",
                params={"shared_Secret": settings.shared_secret},
            )
            if response.status_code == 200 and response.json()["status"] in ("review", "failed"):
                return response.json()["status"]
            await asyncio.sleep(1)
    return "timeout"


@pytest.mark.asyncio
async def test_submit_process_review_flow(test_database, postgres_available, session):
    """Submit -> claim -> process -> review, isolated from prior truncation.

    Previous tests truncate all tables via the ``session`` fixture, which
    wipes the seeded categories/models/prompts. Re-seed the minimum rows
    here so this test is order-independent.
    """
    from sqlalchemy import select

    from app.db.models import Category, Model, Prompt

    if (await session.execute(select(Category).limit(1))).scalars().first() is None:
        code_map = {
            "بهداشت": 24,
            "فناوری اطلاعات و ارتباطات": 16,
            "علوم اجتماعی و رفتاری": 8,
            "حقوق": 11,
            "other": 0,
        }
        for name in (
            "بهداشت", "فناوری اطلاعات و ارتباطات", "علوم اجتماعی و رفتاری",
            "حقوق", "other",
        ):
            session.add(Category(name=name, short_code=code_map[name]))
        await session.commit()
    if (await session.execute(select(Model).where(Model.is_active.is_(True)).limit(1))).scalar_one_or_none() is None:
        session.add(Model(name="e2e-model", provider="openai", version="latest", is_active=True))
        await session.commit()
    if (await session.execute(select(Prompt).where(Prompt.is_active.is_(True)).limit(1))).scalar_one_or_none() is None:
        session.add(Prompt(version="e2e-v1", system_prompt="test prompt", is_active=True))
        await session.commit()

    payload = {
        "shared_Secret": settings.shared_secret,
        "username": "e2e-integration-test",
        "title": "Integration Test Thesis",
        "abstract": "Abstract for the end-to-end integration test.",
        "field": "پزشکی",
        "university": "Test University",
        "table_of_contents": "1. Introduction\n2. Methods\n3. Results",
        "author": "Author",
        "supervisor": "Supervisor",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/submit", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    record_id = body["id"]

    async with async_session() as session:
        job = None
        for _ in range(5):
            candidate = await claim_next_job(session)
            if candidate is None:
                break
            if candidate.id == record_id:
                job = candidate
                break
            candidate.status = "ready"
            await session.commit()

        if job is not None:
            from sqlalchemy import select as sa_select

            from app.db.models import Category, MainCategoryMatch
            from app.schemas.news import CategoryMatchCompact, GeneratedNews

            output = GeneratedNews(
                title="T", lead="L", body="B",
                categories=[
                    CategoryMatchCompact(code=24, score=90, reason="پزشکی است"),
                    CategoryMatchCompact(code=16, score=60, reason="ai"),
                ],
            )
            with patch("app.worker.worker.generate_news") as mock_gen:
                mock_gen.return_value = output
                await process_job(session, job)
            await session.refresh(job)
            assert job.status == "review"
            assert job.output_json == {"title": "T", "lead": "L", "body": "B"}
            assert job.model_id is not None
            assert job.prompt_id is not None
            # The combined call also classified the thesis: two qualifying
            # matches stored, the display primary is the top one.
            matches = (await session.execute(
                sa_select(MainCategoryMatch).where(MainCategoryMatch.main_id == job.id)
            )).scalars().all()
            assert len(matches) == 2
            top = next(m for m in matches if m.rank == 1)
            top_name = (await session.get(Category, top.category_id)).name
            assert top_name == "بهداشت" and top.score == 0.9
            assert job.category_id == top.category_id
        else:
            final_status = await _wait_for_terminal_status(record_id)
            assert final_status == "review"
