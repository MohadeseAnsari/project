"""Auto-publish flow: routing helper, gate-off publishing, unmatched handling,
manual reassignment, and re-publish recovery.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db.models import (
    Category,
    CategoryChannelRoute,
    Channel,
    Delivery,
    Main,
    MainCategoryMatch,
    PanelUser,
    Role,
)
from app.messaging.dispatcher import ensure_pending_deliveries
from app.news.isced import ISCED_CATEGORIES, OTHER_CATEGORY
from app.panel.auth import hash_password
from app.schemas.news import CategoryMatchCompact, GeneratedNews
from app.services import records as record_service
from tests.panel_test_client import panel_client

_spec = importlib.util.spec_from_file_location(
    "auto_route_channels", Path(__file__).resolve().parents[1] / "scripts" / "auto_route_channels.py"
)
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)


def _code_for(name: str) -> int:
    if name == OTHER_CATEGORY:
        return 0
    for i, (n, _) in enumerate(ISCED_CATEGORIES, start=1):
        if n == name:
            return i
    raise KeyError(name)


async def _admin(session: AsyncSession, username: str = "ap-admin"):
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"), role_id=role.id, is_active=True))
    await session.commit()


async def _login(client, username: str = "ap-admin"):
    await client.post("/login", data={"username": username, "password": "secret123"}, follow_redirects=False)


# ---------------------------------------------------------------------------
# Auto-routing script
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_route_matches_by_title(session: AsyncSession):
    cat = Category(name="بهداشت", description="health", short_code=24, deprecated=False)
    ch = Channel(platform="bale", chat_id=1001, title="بهداشت", username="@x", is_active=True)
    session.add_all([cat, ch])
    await session.commit()

    code = await arc.main(False)
    assert code == 0
    rows = (await session.execute(select(CategoryChannelRoute))).scalars().all()
    assert len(rows) == 0

    code = await arc.main(True)
    assert code == 0
    rows = (await session.execute(select(CategoryChannelRoute))).scalars().all()
    assert len(rows) == 1
    assert rows[0].category_id == cat.id and rows[0].channel_id == ch.id


@pytest.mark.asyncio
async def test_auto_route_idempotent(session: AsyncSession):
    cat = Category(name="بهداشت", description="health", short_code=24, deprecated=False)
    ch = Channel(platform="bale", chat_id=1002, title="بهداشت", username="@x", is_active=True)
    session.add_all([cat, ch])
    await session.commit()
    await arc.main(True)
    first = (await session.execute(select(CategoryChannelRoute))).scalars().all()
    code = await arc.main(True)
    assert code == 0
    second = (await session.execute(select(CategoryChannelRoute))).scalars().all()
    assert len(second) == len(first)


@pytest.mark.asyncio
async def test_auto_route_no_match_returns_1(session: AsyncSession):
    cat = Category(name="بهداشت", description="health", short_code=24, deprecated=False)
    ch = Channel(platform="bale", chat_id=1003, title="عنوان نامرتبط", username="@x", is_active=True)
    session.add_all([cat, ch])
    await session.commit()
    code = await arc.main(False)
    assert code == 1
    rows = (await session.execute(select(CategoryChannelRoute))).scalars().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_auto_route_skips_inactive_and_other(session: AsyncSession):
    other = Category(name="other", description="fallback", short_code=0, deprecated=False)
    cat = Category(name="بهداشت", description="health", short_code=24, deprecated=False)
    ch_active = Channel(platform="bale", chat_id=1004, title="other", username="@x", is_active=True)
    ch_inactive = Channel(platform="bale", chat_id=1005, title="بهداشت", username="@x", is_active=False)
    session.add_all([other, cat, ch_active, ch_inactive])
    await session.commit()
    code = await arc.main(True)
    assert code == 0
    rows = (await session.execute(select(CategoryChannelRoute))).scalars().all()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Auto-publish flow (end to end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_publish_creates_pending_deliveries(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    monkeypatch.setattr(settings, "unmatched_category_behavior", "review")

    cat = Category(name="فناوری اطلاعات و ارتباطات", description="ict", short_code=16, deprecated=False)
    other = Category(name=OTHER_CATEGORY, description="fallback", short_code=0, deprecated=False)
    ch = Channel(platform="bale", chat_id=2001, title="ict", username="@c", is_active=True)
    session.add_all([cat, other, ch])
    await session.commit()
    session.add(CategoryChannelRoute(category_id=cat.id, channel_id=ch.id))
    await session.commit()

    job = Main(username="u", title="T", abstract="A", field="f", university="U", author="A", supervisor="S", status="processing")
    session.add(job)
    await session.commit()

    from unittest.mock import patch

    from app.worker.worker import process_job

    output = GeneratedNews(
        title="T", lead="L", body="B",
        categories=[CategoryMatchCompact(code=16, score=90, reason="")],
    )
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    await session.refresh(job)
    matches = (await session.execute(select(MainCategoryMatch).where(MainCategoryMatch.main_id == job.id))).scalars().all()
    assert len(matches) == 1

    created = await ensure_pending_deliveries(session)
    assert created == 1
    delivs = (await session.execute(select(Delivery).where(Delivery.main_id == job.id))).scalars().all()
    assert len(delivs) == 1
    assert delivs[0].status == "pending"


@pytest.mark.asyncio
async def test_unmatched_review_stays_review_no_deliveries(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    monkeypatch.setattr(settings, "unmatched_category_behavior", "review")

    other = Category(name=OTHER_CATEGORY, description="fallback", short_code=0, deprecated=False)
    session.add(other)
    await session.commit()

    job = Main(username="u", title="T", abstract="A", field="f", university="U", author="A", supervisor="S", status="processing")
    session.add(job)
    await session.commit()

    from unittest.mock import patch

    from app.worker.worker import process_job

    output = GeneratedNews(title="T", lead="L", body="B", categories=[])
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    await session.refresh(job)
    assert job.status == "review"
    matches = (await session.execute(select(MainCategoryMatch).where(MainCategoryMatch.main_id == job.id))).scalars().all()
    assert len(matches) == 1 and matches[0].reason == "no auto-match"
    delivs = (await session.execute(select(Delivery).where(Delivery.main_id == job.id))).scalars().all()
    assert len(delivs) == 0


@pytest.mark.asyncio
async def test_unmatched_failed_moves_to_failed(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    monkeypatch.setattr(settings, "unmatched_category_behavior", "failed")

    other = Category(name=OTHER_CATEGORY, description="fallback", short_code=0, deprecated=False)
    session.add(other)
    await session.commit()

    job = Main(username="u", title="T", abstract="A", field="f", university="U", author="A", supervisor="S", status="processing")
    session.add(job)
    await session.commit()

    from unittest.mock import patch

    from app.worker.worker import process_job

    output = GeneratedNews(title="T", lead="L", body="B", categories=[])
    with patch("app.worker.worker.generate_news") as mock_gen:
        mock_gen.return_value = output
        await process_job(session, job)

    await session.refresh(job)
    assert job.status == "failed"
    assert job.error_message == "No category matched above threshold"


# ---------------------------------------------------------------------------
# Manual reassignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_replaces_matches(session: AsyncSession):
    cat1 = Category(name="بهداشت", description="h", short_code=24, deprecated=False)
    cat2 = Category(name="حقوق", description="l", short_code=11, deprecated=False)
    session.add_all([cat1, cat2])
    await session.commit()

    record = Main(username="u", title="T", abstract="A", field="f", university="U", author="A", supervisor="S", status="published", output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(record)
    await session.commit()
    await record_service.store_generated_classification(
        session, record,
        GeneratedNews(title="T", lead="L", body="B",
                     categories=[CategoryMatchCompact(code=24, score=90, reason="r")]),
        model_id=None, prompt_id=None,
    )
    await session.commit()

    await record_service.reassign_category(session, record, cat2.id)
    await session.refresh(record)
    assert record.category_id == cat2.id
    rows = (await session.execute(select(MainCategoryMatch).where(MainCategoryMatch.main_id == record.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].category_id == cat2.id
    assert rows[0].score == 1.0
    assert rows[0].reason == "تعیین دستی توسط بازبین"


# ---------------------------------------------------------------------------
# Re-publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_republish_creates_deliveries_for_new_channels(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    monkeypatch.setattr(settings, "unmatched_category_behavior", "review")

    cat = Category(name="بهداشت", description="h", short_code=24, deprecated=False)
    other = Category(name=OTHER_CATEGORY, description="fallback", short_code=0, deprecated=False)
    ch_old = Channel(platform="bale", chat_id=3001, title="old", username="@o", is_active=True)
    ch_new = Channel(platform="bale", chat_id=3002, title="new", username="@n", is_active=True, activated_at=datetime(2020, 1, 1))
    session.add_all([cat, other, ch_old, ch_new])
    await session.commit()
    session.add(CategoryChannelRoute(category_id=cat.id, channel_id=ch_new.id))
    await session.commit()

    record = Main(username="u", title="T", abstract="A", field="f", university="U", author="A", supervisor="S", status="published", output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(record)
    await session.commit()
    await record_service.store_generated_classification(
        session, record,
        GeneratedNews(title="T", lead="L", body="B",
                     categories=[CategoryMatchCompact(code=24, score=90, reason="")]),
        model_id=None, prompt_id=None,
    )
    await session.commit()

    session.add(Delivery(main_id=record.id, channel_id=ch_old.id, status="sent", platform_message_id=555))
    await session.commit()

    queued = await record_service.republish_record(session, record, "admin")
    assert len(queued) == 1
    assert queued[0]["id"] == ch_new.id
    delivs = (await session.execute(select(Delivery).where(Delivery.main_id == record.id))).scalars().all()
    assert any(d.channel_id == ch_old.id and d.status == "sent" for d in delivs)


# ---------------------------------------------------------------------------
# Panel filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_filters_auto_published_and_needs_classification(session: AsyncSession):
    await _admin(session)
    r1 = Main(username="u", title="auto-published-record", abstract="a", field="f", university="U", author="a", supervisor="s", status="published", approved_at=None, published_at=datetime.utcnow())
    r2 = Main(username="u", title="needs-classification-record", abstract="a", field="f", university="U", author="a", supervisor="s", status="review")
    session.add_all([r1, r2])
    await session.commit()
    cat_other = Category(name="other", description="o", short_code=0, deprecated=False)
    session.add(cat_other)
    await session.commit()
    session.add(MainCategoryMatch(main_id=r2.id, category_id=cat_other.id, score=0.0, rank=1, reason="no auto-match"))
    await session.commit()

    async with panel_client() as client:
        await _login(client)
        ap = await client.get("/records", params={"auto_published": "yes"})
        nc = await client.get("/records", params={"needs_classification": "yes"})
    assert "auto-published-record" in ap.text
    assert "needs-classification-record" not in ap.text
    assert "needs-classification-record" in nc.text
    assert "auto-published-record" not in nc.text