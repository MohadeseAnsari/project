"""The sender queueing an approved article must never park it in the gate.

Regression test for a real race: the sender read the article status from its
SELECT, a reviewer approved in between, and the sender then inserted a fresh
``awaiting_confirmation`` row for an article that could never be approved
again — the delivery was stuck forever even though the record said "approved".
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.dispatcher import confirm_deliveries_for_article, ensure_pending_deliveries
from tests.routing_setup import route_all_active


async def _article(session: AsyncSession, status: str) -> int:
    from app.db.models import Category, Channel, Field, Main, MainCategoryMatch

    category = Category(name="gate-race-cat", description="test")
    session.add(category)
    await session.commit()
    field = Field(name="gate-race-field", normalized_name="gate-race-field")
    channel = Channel(platform="bale", chat_id=8877001, username="@race", is_active=True)
    session.add_all([field, channel])
    await session.commit()
    row = Main(
        username="t", title="Gate race", abstract="A", field="gate-race-field",
        university="U", author="A", supervisor="S", status=status,
        field_id=field.id, category_id=category.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.flush()
    # Routing resolves through the stored matches; this file tests the gate,
    # not the classification, so one qualifying match is enough.
    session.add(MainCategoryMatch(
        main_id=row.id, category_id=category.id, score=0.9, rank=1, reason="test",
    ))
    await session.commit()
    # The category is pointed at every active channel to give the gate
    # something to act on.
    await route_all_active(session, category)
    return row.id


@pytest.mark.asyncio
async def test_gate_on_review_queues_gated(session: AsyncSession, monkeypatch):
    """Unapproved article -> awaiting_confirmation (the gate still holds)."""
    from sqlalchemy import select

    from app.config import settings
    from app.db.models import Delivery

    monkeypatch.setattr(settings, "require_human_confirmation", True)
    await _article(session, "review")
    assert await ensure_pending_deliveries(session) == 1
    row = (await session.execute(select(Delivery))).scalars().one()
    assert row.status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_approved_article_queues_pending_directly(session: AsyncSession, monkeypatch):
    """Approved article -> pending: the decision already happened."""
    from app.config import settings
    from sqlalchemy import select

    from app.db.models import Delivery

    monkeypatch.setattr(settings, "require_human_confirmation", True)
    await _article(session, "approved")
    assert await ensure_pending_deliveries(session) == 1
    row = (await session.execute(select(Delivery))).scalars().one()
    assert row.status == "pending", "an approved article must not queue behind the gate"


@pytest.mark.asyncio
async def test_published_article_queues_pending_for_a_late_channel(
    session: AsyncSession, monkeypatch
):
    """A channel routed after publication still gets its delivery."""
    from app.config import settings
    from sqlalchemy import select

    from app.db.models import Delivery

    monkeypatch.setattr(settings, "require_human_confirmation", True)
    await _article(session, "published")
    assert await ensure_pending_deliveries(session) == 1
    row = (await session.execute(select(Delivery))).scalars().one()
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_race_approval_between_select_and_insert(session: AsyncSession, monkeypatch):
    """The race the fix closes: approve lands while the sender is queueing.

    ``ensure_pending_deliveries`` reads the article list first; we approve the
    article at exactly that moment (by patching the reader). The delivery the
    sender then inserts must be ``pending``, not a permanently-gated row.
    """
    from app.config import settings
    from sqlalchemy import select

    from app.db.models import Delivery, Main
    from app.messaging import dispatcher

    monkeypatch.setattr(settings, "require_human_confirmation", True)
    article_id = await _article(session, "review")

    original = dispatcher.channels_for_article_multi

    async def approve_then_resolve(session, article):
        # Approve (as the panel would) while the sender holds the article list.
        row = await session.get(Main, article_id)
        row.status = "approved"
        await session.commit()
        await confirm_deliveries_for_article(session, article_id)
        return await original(session, article)

    monkeypatch.setattr(dispatcher, "channels_for_article_multi", approve_then_resolve)

    assert await ensure_pending_deliveries(session) == 1
    delivery = (await session.execute(select(Delivery))).scalars().one()
    assert delivery.status == "pending", (
        "delivery queued after approval must not stay in the gate forever"
    )

    # And it can therefore actually be sent.
    from app.messaging.dispatcher import claim_next_delivery

    assert await claim_next_delivery(session) is not None