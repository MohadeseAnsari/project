"""The v2-combined single-call pipeline: schema, worker, routing, migration.

ONE PydanticAI call per thesis answers with ``GeneratedNews{title, lead, body,
categories}``; the Worker post-processes ``categories`` (hallucination filter
against the live ``categories`` rows, score clamping, dedupe, threshold/rank
trimming), stores the result in ``main_category_matches``, and routing fans
the article out over the qualifying matches. Nothing qualifies → exactly one
``other`` row, no deliveries.

This file pins each stage of that contract, plus the migration pieces the
truncated-``session`` fixture would wipe (vocabulary, prompt seed, routes).
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.connection import async_session
from app.db.models import (
    Category,
    CategoryChannelRoute,
    Channel,
    Delivery,
    Main,
    MainCategoryMatch,
    Prompt,
)
from app.messaging.dispatcher import (
    ensure_pending_deliveries,
)
from app.news.isced import ISCED_CATEGORY_NAMES, OTHER_CATEGORY
from app.schemas.news import CategoryMatchCompact, GeneratedNews

OUTPUT = {"title": "تیتر", "lead": "لید", "body": "متن کامل"}


async def _seed_vocabulary(session: AsyncSession, *names: str) -> dict[str, Category]:
    """Create live category rows (worker tests truncate the seed)."""
    from app.news.isced import ISCED_CATEGORIES

    code_map = {name: i + 1 for i, (name, _) in enumerate(ISCED_CATEGORIES)}
    code_map[OTHER_CATEGORY] = 0
    rows = {}
    for name in names:
        rows[name] = Category(
            name=name, description=f"desc {name}", short_code=code_map.get(name),
        )
        session.add(rows[name])
    await session.commit()
    return rows


async def _seed_worker_world(session: AsyncSession):
    """Three bale channels, created before any article (activation friendly)."""
    channels = SimpleNamespace()
    for index in range(3):
        channel = Channel(platform="bale", chat_id=70000 + index, username=f"@c{index}")
        session.add(channel)
        setattr(channels, f"c{index}", channel)
    await session.commit()
    return channels


class _JobBuilder:
    """Build a claimable job and run the worker with a stubbed LLM."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def run(self, categories: list[dict], **job_kwargs) -> Main:
        from unittest.mock import patch

        from app.worker.worker import process_job

        job = Main(
            username="tester", title="T", abstract="A",
            field=job_kwargs.pop("field", "پزشکی"),
            university="U", author="A", supervisor="S",
            status=job_kwargs.pop("status", "processing"),  # as claim_next_job leaves it
            **job_kwargs,
        )
        self.session.add(job)
        await self.session.commit()

        from app.news.isced import ISCED_CATEGORIES

        code_map = {name: i + 1 for i, (name, _) in enumerate(ISCED_CATEGORIES)}
        code_map[OTHER_CATEGORY] = 0

        compact = []
        for c in categories:
            if "code" in c:
                compact.append(CategoryMatchCompact(
                    code=c["code"], score=c.get("score", 0), reason=c.get("reason", ""),
                ))
            else:
                name = c.get("category")
                code = code_map.get(name)
                if code is None:
                    continue  # hallucinated name → dropped, as the worker does
                score = c.get("score", 0)
                if isinstance(score, float):
                    score = int(round(score * 100))
                compact.append(CategoryMatchCompact(
                    code=code, score=score, reason=c.get("reason", ""),
                ))

        output = GeneratedNews(
            title="تیتر", lead="لید", body="متن",
            categories=compact,
        )
        with patch("app.worker.worker.generate_news") as mock_gen:
            mock_gen.return_value = output
            await process_job(self.session, job)
        await self.session.refresh(job)
        return job


async def _matches(session: AsyncSession, main_id: int) -> list[tuple[int, float, int, str, int]]:
    """Stored rows: (category_id, score, rank, reason, model_id|None)."""
    rows = (
        await session.execute(
            select(
                MainCategoryMatch.category_id,
                MainCategoryMatch.score,
                MainCategoryMatch.rank,
                MainCategoryMatch.reason,
                MainCategoryMatch.model_id,
            ).where(MainCategoryMatch.main_id == main_id)
        )
    ).all()
    return [tuple(r) for r in rows]


# ==========================================================================
# 1. schema layer
# ==========================================================================


def test_generatednews_defaults_to_no_categories():
    output = GeneratedNews(title="t", lead="l", body="b")
    assert output.categories == []


def test_generatednews_caps_categories_at_three():
    match = CategoryMatchCompact(code=24, score=90, reason="r")
    GeneratedNews(title="t", lead="l", body="b", categories=[match] * 3)  # ok
    with pytest.raises(ValidationError):
        GeneratedNews(title="t", lead="l", body="b", categories=[match] * 4)


# ---------------------------------------------------------------------------
# Compact schema (Optimization A/B/C)
# ---------------------------------------------------------------------------


def test_categorymatchcompact_valid():
    from app.schemas.news import CategoryMatchCompact

    m = CategoryMatchCompact(code=16, score=95, reason="")
    assert m.code == 16 and m.score == 95 and m.reason == ""


def test_categorymatchcompact_code_out_of_range():
    from pydantic import ValidationError

    from app.schemas.news import CategoryMatchCompact

    with pytest.raises(ValidationError):
        CategoryMatchCompact(code=99, score=95)
    with pytest.raises(ValidationError):
        CategoryMatchCompact(code=-1, score=95)


def test_categorymatchcompact_score_out_of_range():
    from pydantic import ValidationError

    from app.schemas.news import CategoryMatchCompact

    with pytest.raises(ValidationError):
        CategoryMatchCompact(code=16, score=150)
    with pytest.raises(ValidationError):
        CategoryMatchCompact(code=16, score=-5)


def test_categorymatchcompact_reason_truncated():
    from app.schemas.news import CategoryMatchCompact

    m = CategoryMatchCompact(code=16, score=95, reason="a" * 200)
    assert len(m.reason) <= 80
    assert m.reason.endswith("...")


def test_prompts_embed_the_exact_vocabulary():
    """The system prompt must contain all 29 names + the fallback."""
    from app.news.prompts import PROMPT_VERSION, SYSTEM_PROMPT

    assert PROMPT_VERSION == "v3-compact"
    for name in ISCED_CATEGORY_NAMES:
        assert name in SYSTEM_PROMPT, f"{name} missing from the combined prompt"
    assert "score" in SYSTEM_PROMPT or "نمره" in SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_store_filters_hallucinations_clamps_and_dedupes(session: AsyncSession):
    from app.services.records import normalize_llm_categories

    vocab = await _seed_vocabulary(session, "بهداشت", "حقوق")
    other = Category(name=OTHER_CATEGORY)
    session.add(other)
    await session.commit()

    # Clamping + dedupe (keep-highest), all within the schema's 3-item cap.
    output = GeneratedNews(
        title="t", lead="l", body="b",
        categories=[
            CategoryMatchCompact(code=vocab["بهداشت"].short_code, score=100, reason="too high"),   # clamp → 1.0
            CategoryMatchCompact(code=vocab["بهداشت"].short_code, score=20, reason="dup"),        # dedupe keeps 1.0
            CategoryMatchCompact(code=vocab["حقوق"].short_code, score=0, reason="negative"),     # clamp → 0.0
        ],
    )
    validated = await normalize_llm_categories(session, output)
    assert [(v["category"], v["score"]) for v in validated] == [
        ("بهداشت", 1.0),
        ("حقوق", 0.0),
    ]
    ids = {v["category_id"] for v in validated}
    assert ids == {vocab["بهداشت"].id, vocab["حقوق"].id}

    # Hallucinated codes (not live category rows) are dropped wholesale.
    output = GeneratedNews(
        title="t", lead="l", body="b",
        categories=[
            CategoryMatchCompact(code=5, score=99, reason="invented"),  # not seeded
            CategoryMatchCompact(code=vocab["بهداشت"].short_code, score=70, reason="real"),
        ],
    )
    validated = await normalize_llm_categories(session, output)
    assert [(v["category"], v["score"]) for v in validated] == [("بهداشت", 0.7)]


@pytest.mark.asyncio
async def test_store_rejects_more_than_three_matches(session: AsyncSession):
    """The schema caps the LLM answer at 3 — validation, not silent trim."""
    match = CategoryMatchCompact(code=24, score=90, reason="r")
    with pytest.raises(ValidationError):
        normalize = GeneratedNews(title="t", lead="l", body="b", categories=[match] * 4)
        del normalize


@pytest.mark.asyncio
async def test_store_empty_answer_falls_back_to_other_with_reason(session: AsyncSession):
    await _seed_vocabulary(session, OTHER_CATEGORY)
    await _seed_worker_world(session)
    job = await _JobBuilder(session).run(categories=[])

    rows = await _matches(session, job.id)
    assert len(rows) == 1
    category_id, score, rank, reason, _model_id = rows[0]
    other = (await session.execute(
        select(Category).where(Category.name == OTHER_CATEGORY)
    )).scalar_one()
    assert category_id == other.id
    assert score == 0.0 and rank == 1 and reason == "no auto-match"
    assert job.category_id == other.id
    # No deliveries follow a fallback-only record — even with an active
    # channel sitting there.
    assert await ensure_pending_deliveries(session) == 0
    assert (await session.execute(select(Delivery))).scalars().all() == []  # noqa: F841


@pytest.mark.asyncio
async def test_regeneration_replaces_the_stored_matches(session: AsyncSession):
    """Regenerating must not mix answers (or stack duplicate rows)."""
    from app.services.records import store_generated_classification

    await _seed_vocabulary(session, "بهداشت", "حقوق", OTHER_CATEGORY)
    job = await _JobBuilder(session).run(
        categories=[{"category": "بهداشت", "score": 0.9, "reason": "first run"}]
    )
    assert len(await _matches(session, job.id)) == 1

    second = GeneratedNews(
        title="t", lead="l", body="b",
        categories=[{"code": 11, "score": 80, "reason": "second run"}],
    )
    await store_generated_classification(session, job, second, model_id=None, prompt_id=None)
    await session.commit()

    rows = await _matches(session, job.id)
    assert len(rows) == 1
    health = (await session.execute(
        select(Category).where(Category.name == "بهداشت")
    )).scalar_one()
    rights = (await session.execute(
        select(Category).where(Category.name == "حقوق")
    )).scalar_one()
    assert rows[0][0] == rights.id
    assert rows[0][0] != health.id
    assert job.category_id == rights.id


@pytest.mark.asyncio
async def test_threshold_and_top_n_apply(monkeypatch, session: AsyncSession):
    """Only score >= MIN_CATEGORY_RELEVANCE, at most TOP_N_CATEGORIES."""
    monkeypatch.setattr(settings, "min_category_relevance", 0.5)
    monkeypatch.setattr(settings, "top_n_categories", 2)

    await _seed_vocabulary(session, "بهداشت", "حقوق", "معماری و ساخت‌وساز", OTHER_CATEGORY)
    await _seed_worker_world(session)
    job = await _JobBuilder(session).run(categories=[
        {"category": "بهداشت", "score": 0.9, "reason": "r1"},
        {"category": "حقوق", "score": 0.55, "reason": "r2"},
        {"category": "معماری و ساخت‌وساز", "score": 0.4, "reason": "below threshold"},
    ])

    rows = await _matches(session, job.id)
    assert len(rows) == 2, "the 0.4 match must not be stored"
    assert [r[2] for r in sorted(rows, key=lambda r: r[2])] == [1, 2]


@pytest.mark.asyncio
async def test_worker_maps_compact_codes(session: AsyncSession):
    """Compact LLM output (codes 0-29) maps to full DB rows."""

    vocab = await _seed_vocabulary(session, "بهداشت", "فناوری اطلاعات و ارتباطات", OTHER_CATEGORY)
    await _seed_worker_world(session)
    job = await _JobBuilder(session).run(categories=[
        {"code": vocab["فناوری اطلاعات و ارتباطات"].short_code, "score": 80, "reason": "ML imaging"},
        {"code": vocab["بهداشت"].short_code, "score": 70, "reason": "تشخیص پزشکی"},
    ])
    rows = await _matches(session, job.id)
    assert len(rows) == 2
    # scores converted to 0.0-1.0
    scores = sorted(r[1] for r in rows)
    assert scores == [0.70, 0.80]
    # category ids mapped
    ids = {r[0] for r in rows}
    assert ids == {vocab["فناوری اطلاعات و ارتباطات"].id, vocab["بهداشت"].id}
    top = next(r for r in rows if r[2] == 1)
    assert top[1] == 0.80
    assert top[3] == "ML imaging"


@pytest.mark.asyncio
async def test_worker_filters_invalid_compact_code(session: AsyncSession):

    await _seed_vocabulary(session, OTHER_CATEGORY)
    await _seed_worker_world(session)
    job = await _JobBuilder(session).run(categories=[
        {"code": 5, "score": 90, "reason": "bad"},
    ])
    rows = await _matches(session, job.id)
    assert len(rows) == 1
    assert rows[0][1] == 0.0  # other fallback
    assert rows[0][3] == "no auto-match"


@pytest.mark.asyncio
async def test_worker_filters_low_score_compact(session: AsyncSession):

    vocab = await _seed_vocabulary(session, "بهداشت", OTHER_CATEGORY)
    await _seed_worker_world(session)
    job = await _JobBuilder(session).run(categories=[
        {"code": vocab["بهداشت"].short_code, "score": 30, "reason": "low"},
    ])
    rows = await _matches(session, job.id)
    assert len(rows) == 1
    assert rows[0][1] == 0.0  # filtered → other fallback


@pytest.mark.asyncio
async def test_worker_dedupes_compact_code(session: AsyncSession):

    vocab = await _seed_vocabulary(session, "بهداشت", OTHER_CATEGORY)
    await _seed_worker_world(session)
    job = await _JobBuilder(session).run(categories=[
        {"code": vocab["بهداشت"].short_code, "score": 60, "reason": "low"},
        {"code": vocab["بهداشت"].short_code, "score": 90, "reason": "high"},
    ])
    rows = await _matches(session, job.id)
    assert len(rows) == 1
    assert rows[0][1] == 0.9  # kept highest


@pytest.mark.asyncio
async def test_other_match_never_qualifies_even_at_zero_threshold(
    monkeypatch, session: AsyncSession
):
    """``other`` is a fallback, never an auto-delivery source."""
    monkeypatch.setattr(settings, "min_category_relevance", 0.0)

    await _seed_vocabulary(session, "بهداشت", OTHER_CATEGORY)
    await _seed_worker_world(session)
    job = await _JobBuilder(session).run(
        categories=[{"category": "بهداشت", "score": 0.9, "reason": "real match"}]
    )
    rows = await _matches(session, job.id)
    assert len(rows) == 1 and rows[0][3] != "no auto-match"
    assert job.category_id == rows[0][0]

    # Same call answering ONLY "other" (even with a positive score) falls back.
    job2 = await _JobBuilder(session).run(
        categories=[{"category": OTHER_CATEGORY, "score": 0.9, "reason": "fallback named"}]
    )
    rows2 = await _matches(session, job2.id)
    assert len(rows2) == 1
    assert rows2[0][2] == 1.0 or rows2[0][1] == 0.0
    assert rows2[0][3] == "no auto-match", "the named match must be ignored"


# ==========================================================================
# 2. worker end-to-end (queue → delivery provenance)
# ==========================================================================


@pytest.mark.asyncio
async def test_ensure_delivers_per_category_and_snapshots_top1(session: AsyncSession):
    """Three categories → union of channels; the row records the top-1."""
    health = (await _seed_vocabulary(
        session, "بهداشت", "حقوق", "معماری و ساخت‌وساز", OTHER_CATEGORY
    ))
    channels = await _seed_worker_world(session)
    for cat_name, channel_indices in (("بهداشت", (0, 1)), ("حقوق", (1, 2)), ("معماری و ساخت‌وساز", (2,))):
        for index in channel_indices:
            session.add(CategoryChannelRoute(
                category_id=health[cat_name].id, channel_id=getattr(channels, f"c{index}").id,
            ))
    await session.commit()

    job = await _JobBuilder(session).run(categories=[
        {"category": "بهداشت", "score": 0.9, "reason": "r1"},
        {"category": "حقوق", "score": 0.7, "reason": "r2"},
    ])
    created = await ensure_pending_deliveries(session)
    assert created == 3  # c0 (بهداشت), c1 (union), c2 (حقوق)
    rows = (await session.execute(
        select(Delivery, Channel)
        .join(Channel, Channel.id == Delivery.channel_id)
        .where(Delivery.main_id == job.id)
    )).all()
    by_channel = {channel.id: delivery for delivery, channel in rows}
    assert set(by_channel) == {channels.c0.id, channels.c1.id, channels.c2.id}
    # Provenance snapshot = top-1 match's category, for every row (limitation).
    assert all(
        d.category_id == health["بهداشت"].id for d in by_channel.values()
    )


@pytest.mark.asyncio
async def test_ensure_respects_the_human_gate(monkeypatch, session: AsyncSession):
    """review + gate on → awaiting_confirmation; gate off → pending."""
    health = await _seed_vocabulary(session, "بهداشت", OTHER_CATEGORY)
    channels = await _seed_worker_world(session)
    session.add(CategoryChannelRoute(
        category_id=health["بهداشت"].id, channel_id=channels.c0.id,
    ))
    await session.commit()

    before = settings.require_human_confirmation
    # Gate ON → awaiting_confirmation.
    monkeypatch.setattr(settings, "require_human_confirmation", True)
    await _JobBuilder(session).run(
        categories=[{"category": "بهداشت", "score": 0.9, "reason": "r"}]
    )
    assert await ensure_pending_deliveries(session) == 1
    row = (await session.execute(select(Delivery))).scalar_one()
    assert row.status == "awaiting_confirmation"

    # Gate OFF → pending directly.
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    await _JobBuilder(session).run(
        categories=[{"category": "بهداشت", "score": 0.9, "reason": "r"}]
    )
    assert await ensure_pending_deliveries(session) == 1
    rows = (await session.execute(select(Delivery))).scalars().all()
    assert {r.status for r in rows} == {"awaiting_confirmation", "pending"}
    monkeypatch.setattr(settings, "require_human_confirmation", before)


# ==========================================================================
# 3. API surface
# ==========================================================================


@pytest.mark.asyncio
async def test_api_classification_and_manual_override(session: AsyncSession):
    from app.main import app

    health = await _seed_vocabulary(session, "بهداشت", OTHER_CATEGORY)
    job = await _JobBuilder(session).run(
        categories=[{"category": "بهداشت", "score": 0.9, "reason": "model said"}]
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # unauthorized
        r = await client.get(f"/records/{job.id}/classification", params={"shared_Secret": "wrong"})
        assert r.status_code == 401

        # the LLM answer, as stored
        r = await client.get(
            f"/records/{job.id}/classification", params={"shared_Secret": settings.shared_secret}
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload["record_id"] == job.id
        assert [m["category"] for m in payload["matches"]] == ["بهداشت"]
        assert payload["matches"][0]["is_primary"] is True
        assert payload["matches"][0]["score"] == 0.9

        # manual override replaces the matches entirely
        r = await client.post(
            f"/records/{job.id}/category-matches",
            json={
                "shared_Secret": settings.shared_secret,
                "matches": [
                    {"category": OTHER_CATEGORY, "score": 0.3, "reason": "reviewer cleared it"},
                    {"category": "بهداشت", "score": 0.6, "reason": "keep"},
                ],
            },
        )
        assert r.status_code == 200
        payload = r.json()
        assert [m["category"] for m in payload["matches"]] == ["بهداشت", "other"], (
            "sorted by score, deduped against the fallback rule"
        )
        assert payload["matches"][0]["is_primary"] is True
        assert payload["category_id"] == health["بهداشت"].id

        # unknown category name → 422, nothing replaced
        r = await client.post(
            f"/records/{job.id}/category-matches",
            json={
                "shared_Secret": settings.shared_secret,
                "matches": [{"category": "دستهٔ موهومی", "score": 1.0}],
            },
        )
        assert r.status_code == 422

        # out of range score → 422 (schema validation)
        r = await client.post(
            f"/records/{job.id}/category-matches",
            json={
                "shared_Secret": settings.shared_secret,
                "matches": [{"category": "بهداشت", "score": 1.5}],
            },
        )
        assert r.status_code == 422

        # stats counted by PRIMARY match
        r = await client.get("/stats", params={"shared_Secret": settings.shared_secret})
        assert r.status_code == 200
        assert r.json()["records_by_category"] == {"بهداشت": 1}

    rows = await _matches(session, job.id)
    assert len(rows) == 2, "override replaced the LLM answer with 2 rows"
    assert all(r[4] is None for r in rows), "manual rows are NOT attributed to the model"


@pytest.mark.asyncio
async def test_manual_override_clearing_to_the_fallback(session: AsyncSession):
    """An empty matches list must never leave the record unclassified."""
    from app.services.records import replace_category_matches

    await _seed_vocabulary(session, "بهداشت", OTHER_CATEGORY)
    job = await _JobBuilder(session).run(
        categories=[{"category": "بهداشت", "score": 0.9, "reason": "r"}]
    )
    record = await replace_category_matches(session, job.id, [])
    other = (await session.execute(
        select(Category).where(Category.name == OTHER_CATEGORY)
    )).scalar_one()
    assert record.category_id == other.id
    rows = await _matches(session, job.id)
    assert rows[0][3] == "no auto-match"


@pytest.mark.asyncio
async def test_panel_manual_assign_writes_one_manual_match(session: AsyncSession):
    """The panel correction replaces the matches so routing follows it."""

    from app.db.models import AuditLog, PanelUser, Role
    from app.panel.auth import hash_password
    from tests.panel_test_client import panel_client

    health = await _seed_vocabulary(session, "بهداشت", "حقوق", OTHER_CATEGORY)
    channels = await _seed_worker_world(session)
    session.add(CategoryChannelRoute(category_id=health["حقوق"].id, channel_id=channels.c0.id))
    await session.commit()

    role = (await session.execute(select(Role).where(Role.name == "editor"))).scalar_one_or_none()
    if role is None:
        role = Role(name="editor", description="editor")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="cat-reviewer", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()

    job = await _JobBuilder(session).run(
        categories=[{"category": "بهداشت", "score": 0.9, "reason": "model said"}]
    )

    async with panel_client() as client:
        await client.post("/login", data={"username": "cat-reviewer", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/records/{job.id}/category", data={"category_id": str(health["حقوق"].id)},
            follow_redirects=False,
        )
        assert response.status_code == 302

    await session.refresh(job)
    rows = await _matches(session, job.id)
    assert len(rows) == 1
    assert rows[0][0] == health["حقوق"].id
    assert rows[0][1] == 1.0
    assert rows[0][3] == "تعیین دستی توسط بازبین"
    assert job.category_id == health["حقوق"].id

    # Context: the earlier LLM match is GONE (replaced), the audit trail notes it.
    actions = (await session.execute(
        select(AuditLog.action).where(AuditLog.action == "reassign_category")
    )).scalars().all()
    assert len(actions) == 1

    # And routing now follows the manual match.
    created = await ensure_pending_deliveries(session)
    assert created == 1


# ==========================================================================
# 4. the migration itself (migrated DB WITHOUT truncation)
# ==========================================================================


@pytest_asyncio.fixture
async def migrated_db():
    """A FRESH database with migrations 001..023 applied from scratch.

    The conftest ``session`` pattern truncates everything each test, so the
    migration's OWN seeded rows (ISCED vocabulary, v2-combined prompt, routes)
    never survive to a later test. These tests need the post-migration state
    itself, so they rebuild the test database, apply every migration, and run
    against it. The DB is left rebuilt (other tests simply truncate again).
    """
    from pathlib import Path

    import asyncpg

    from app.db.connection import engine
    from tests.conftest import TEST_DB_NAME, _admin_url, _pg_url

    admin = await asyncpg.connect(_admin_url("postgres"))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            TEST_DB_NAME,
        )
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        await admin.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    finally:
        await admin.close()

    conn = await asyncpg.connect(_pg_url(TEST_DB_NAME))
    try:
        for migration_file in sorted(Path("migrations").glob("*.sql")):
            await conn.execute(migration_file.read_text(encoding="utf-8"))
    finally:
        await conn.close()

    # The pool may still carry connections to the DB we just replaced.
    await engine.dispose()
    yield


@pytest.mark.asyncio
async def test_migration_seeds_the_exact_isced_vocabulary(migrated_db):
    """All 29 names + ``other`` exist, live, character for character."""
    async with async_session() as s:
        rows = (await s.execute(select(Category))).scalars().all()
        by_name = {row.name: row for row in rows}

        for name in ISCED_CATEGORY_NAMES:
            assert name in by_name, f"missing ISCED category: {name}"
            assert by_name[name].deprecated is False, f"{name} must be live"

        # Migration 024 deleted the pre-ISCED vocabulary after remapping its
        # fields/records, so only the 30 live rows may remain.
        for legacy in ("پزشکی و علوم سلامت", "ورزش و تربیت بدنی", "کامپیوتر و فناوری اطلاعات"):
            assert legacy not in by_name, f"legacy category survived: {legacy}"
        assert all(row.deprecated is False for row in rows)
        assert len(rows) == len(ISCED_CATEGORY_NAMES), "vocabulary must be exactly 29 + other"
        # دامپزشکی was both a legacy and an ISCED name — it stays live.
        assert by_name["دامپزشکی"].deprecated is False

    # And the SQL artifact itself carries every name (drift guard).
    from pathlib import Path

    sql_text = Path("migrations/023_isced_categories.sql").read_text(encoding="utf-8")
    for name in ISCED_CATEGORY_NAMES:
        assert name in sql_text, f"{name} missing from the migration file"


@pytest.mark.asyncio
async def test_migration_populates_short_codes(migrated_db):
    """Optimization A: every live category has a unique 0-29 short_code."""
    async with async_session() as s:
        rows = (await s.execute(
            select(Category.name, Category.short_code).where(Category.short_code.isnot(None))
        )).all()
        assert len(rows) == 30
        by_name = {name: code for name, code in rows}
        assert by_name["other"] == 0
        assert by_name["بهداشت"] == 24
        assert by_name["برنامه‌ها و مدارک پایه"] == 1
        assert by_name["خدمات حمل‌ونقل"] == 29
        # uniqueness
        codes = [code for _, code in rows]
        assert len(set(codes)) == len(codes)


@pytest.mark.asyncio
async def test_migration_seeds_and_activates_the_v2_prompt(migrated_db):
    """The active prompt row IS v2-combined and equals the packaged prompt.

    Without it, a fresh deployment's resolution chain would run the old v1
    prompt (which never asks for categories) and every classification would
    come back empty.
    """
    from app.news.prompts import PROMPT_VERSION, SYSTEM_PROMPT

    async with async_session() as s:
        prompts = (await s.execute(select(Prompt))).scalars().all()
        by_version = {p.version: p for p in prompts}
        assert PROMPT_VERSION in by_version
        v3 = by_version[PROMPT_VERSION]
        assert v3.is_active is True
        assert v3.system_prompt == SYSTEM_PROMPT, "DB and packaged prompt must not drift"
        assert "v2-combined" in by_version, "the old prompt row stays for audit"
        assert by_version["v2-combined"].is_active is False


@pytest.mark.asyncio
async def test_migration_keeps_routes_and_seeds_new_categories(migrated_db):
    """Legacy routes survive untouched; every live category can still route."""
    async with async_session() as s:
        rows = (await s.execute(
            select(CategoryChannelRoute.category_id, Category.name, Category.deprecated)
            .join(Category, Category.id == CategoryChannelRoute.category_id)
        )).all()
        routed_names = {name for _cid, name, _dep in rows}

        # New vocabulary: seeded explicitly by 023 (or inherited by 002's
        # دامپزشکی) → each one reaches the seeded channel.
        for name in ISCED_CATEGORY_NAMES:
            assert name in routed_names, f"{name} must have a seeded route"

        # The retired vocabulary is gone (migration 024), so its routes are gone
        # too; every routed category is a live ISCED row.
        assert "ورزش و تربیت بدنی" not in routed_names
        assert routed_names <= set(ISCED_CATEGORY_NAMES)


@pytest.mark.asyncio
async def test_migrated_db_classifies_and_delivers_end_to_end(migrated_db):
    """On the REAL seeded vocabulary, the whole loop runs without extra seeds.

    The worker's schema max_length guard is the only size limit here; the
    settings-driven trim is asserted above.
    """
    from unittest.mock import patch

    from app.worker.worker import claim_next_job, process_job

    async with async_session() as s:
        health = (await s.execute(
            select(Category).where(Category.name == "بهداشت")
        )).scalar_one()
        job = Main(username="t", title="T", abstract="A", field="پزشکی",
                   university="U", author="A", supervisor="S", status="ready")
        s.add(job)
        await s.commit()

        # Claim exactly like the worker does (ready -> processing).
        claimed = await claim_next_job(s)
        assert claimed is not None and claimed.id == job.id
        job = claimed

        output = GeneratedNews(
            title="t", lead="l", body="b",
            categories=[CategoryMatchCompact(code=24, score=80, reason="medical")],
        )
        with patch("app.worker.worker.generate_news") as mock_gen:
            mock_gen.return_value = output
            await process_job(s, job)

        await s.refresh(job)
        assert job.status == "review"
        assert job.category_id == health.id
        rows = await _matches(s, job.id)
        assert len(rows) == 1 and rows[0][1] == 0.8
