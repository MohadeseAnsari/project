"""The lifecycle backfill migrations (016/017) derive truth from real data.

These run against a freshly migrated database, so each test rebuilds the
pre-migration shape (deliveries sent before ``published_at`` existed) and
asserts what the SQL does to it.
"""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def _apply(filename: str) -> None:
    """Execute one migration file with raw asyncpg, exactly as the runner does.

    Uses a separate connection because migrations use ``ALTER TABLE ... DROP
    CONSTRAINT``, which cannot run inside the test's active transaction.
    """
    import asyncpg

    conn = await asyncpg.connect(_pg_test_url())
    try:
        await conn.execute(Path(f"migrations/{filename}").read_text(encoding="utf-8"))
    finally:
        await conn.close()


def _pg_test_url() -> str:
    """The test database URL as plain asyncpg wants it (no ``+asyncpg``)."""
    from tests.conftest import _PG_URL

    return _PG_URL


async def _make_article(session: AsyncSession, *, title: str, delivery_status: str,
                        approved_at=None, published_at=None, status="review") -> int:
    """Insert an article plus one delivery row directly (no services)."""
    from datetime import datetime

    row = await session.execute(
        text(
            """
            INSERT INTO main (username, title, abstract, field, university, author,
                              supervisor, status, output_json, approved_at, published_at, created_at, updated_at)
            VALUES ('t', :title, 'A', 'f', 'U', 'A', 'S', :status,
                    '{"title":"T","lead":"L","body":"B"}'::jsonb, :approved_at, :published_at,
                    now(), now())
            RETURNING id
            """
        ),
        {"title": title, "status": status, "approved_at": approved_at, "published_at": published_at},
    )
    main_id = row.scalar_one()
    channel = await session.execute(
        text("INSERT INTO channels (platform, chat_id, username) VALUES ('bale', :chat, '@c') RETURNING id"),
        {"chat": abs(hash(title)) % 10_000_000 + 1},
    )
    channel_id = channel.scalar_one()
    await session.execute(
        text(
            """
            INSERT INTO deliveries (main_id, channel_id, status, attempts, platform_message_id,
                                    next_attempt_at, sent_at, created_at, updated_at)
            VALUES (:main_id, :channel_id, :status, 1, 42, now(), :sent_at, now(), now())
            """
        ),
        {
            "main_id": main_id,
            "channel_id": channel_id,
            "status": delivery_status,
            "sent_at": datetime(2026, 1, 1, 12, 0, 0) if delivery_status == "sent" else None,
        },
    )
    await session.commit()
    return main_id


@pytest.mark.asyncio
async def test_016_backfills_from_timestamps(session: AsyncSession):
    """016: an approved/published timestamp moves the status to match."""
    from datetime import datetime

    approved_id = await _make_article(
        session, title="t016-approved", delivery_status="pending",
        approved_at=datetime(2026, 2, 1, 9, 0, 0),
    )
    plain_id = await _make_article(session, title="t016-plain", delivery_status="pending")
    # The migration runs over rows that already exist, as it does in production.
    await _apply("016_record_approved_published.sql")

    rows = (
        await session.execute(
            text("SELECT id, status FROM main WHERE id = ANY(:ids)"),
            {"ids": [approved_id, plain_id]},
        )
    ).all()
    statuses = dict(rows)
    assert statuses[approved_id] == "approved"
    assert statuses[plain_id] == "review"


@pytest.mark.asyncio
async def test_017_publishes_fully_delivered_articles(session: AsyncSession):
    """017: all-sent deliveries publish the article, even with no timestamps."""
    # The reported bug: sent long ago, no published_at, still reading 'review'.
    stale_id = await _make_article(session, title="t017-stale", delivery_status="sent")
    await _apply("017_backfill_published_from_deliveries.sql")

    statuses = (
        await session.execute(
            text("SELECT status, published_at FROM main WHERE id = :id"), {"id": stale_id}
        )
    ).one()
    assert statuses[0] == "published"
    assert statuses[1] is not None, "published_at must be back-filled, not left NULL"


@pytest.mark.asyncio
async def test_017_uses_the_earliest_send_time(session: AsyncSession):
    """The backfilled published_at is the real send time, not 'now'."""

    stale_id = await _make_article(session, title="t017-time", delivery_status="sent")
    await _apply("017_backfill_published_from_deliveries.sql")
    sent_at = (
        await session.execute(
            text("SELECT sent_at FROM deliveries WHERE main_id = :id"), {"id": stale_id}
        )
    ).scalar_one()
    published_at = (
        await session.execute(
            text("SELECT published_at FROM main WHERE id = :id"), {"id": stale_id}
        )
    ).scalar_one()
    assert published_at == sent_at


@pytest.mark.asyncio
async def test_017_leaves_partially_delivered_alone(session: AsyncSession):
    """An article with any unsent delivery is not published yet."""
    partial_id = await _make_article(session, title="t017-partial", delivery_status="failed")
    await _apply("017_backfill_published_from_deliveries.sql")

    status, published_at = (
        await session.execute(
            text("SELECT status, published_at FROM main WHERE id = :id"), {"id": partial_id}
        )
    ).one()
    assert status == "review"
    assert published_at is None


@pytest.mark.asyncio
async def test_017_never_rewrites_a_rejection(session: AsyncSession):
    """A human rejection stays final even when a delivery had been sent."""
    rejected_id = await _make_article(
        session, title="t017-rejected", delivery_status="sent", status="rejected"
    )
    await _apply("017_backfill_published_from_deliveries.sql")
    status = (
        await session.execute(
            text("SELECT status FROM main WHERE id = :id"), {"id": rejected_id}
        )
    ).scalar_one()
    assert status == "rejected"


@pytest.mark.asyncio
async def test_no_row_contradicts_its_deliveries_after_backfill(session: AsyncSession):
    """The invariant the panel relies on, asserted over the whole table."""
    await _make_article(session, title="t017-inv-1", delivery_status="sent")
    await _make_article(session, title="t017-inv-2", delivery_status="failed")
    await _apply("017_backfill_published_from_deliveries.sql")

    offenders = (
        await session.execute(
            text(
                """
                SELECT m.id, m.status
                FROM main m
                JOIN deliveries d ON d.main_id = m.id
                WHERE m.title LIKE 't017-inv-%'
                GROUP BY m.id, m.status
                HAVING count(d.id) = sum(CASE WHEN d.status = 'sent' THEN 1 ELSE 0 END)
                   AND m.status <> 'published'
                   AND m.status <> 'rejected'
                """
            )
        )
    ).all()
    assert offenders == [], f"rows contradict their deliveries: {offenders}"