"""Tests for the human confirmation gate on delivery queueing.

When ``settings.require_human_confirmation`` is True (default), newly queued
deliveries must wait as ``awaiting_confirmation`` so nothing publishes without
approval. When False, queueing writes ``pending`` directly (pre-gate behaviour).
"""

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Category, Channel, Delivery, Field, Main, MainCategoryMatch
from tests.routing_setup import route_category
from app.messaging.dispatcher import (
    claim_next_delivery,
    confirm_deliveries_for_article,
    ensure_pending_deliveries,
)

OUTPUT = {"title": "عنوان", "lead": "لید", "body": "متن کامل"}

_field_counter = itertools.count(1000)


async def _category(session: AsyncSession) -> Category:
    cat = await session.get(Category, 1)
    if cat is None:
        cat = Category(name="پزشکی و علوم سلامت", description="Medical")
        session.add(cat)
        await session.commit()
    return cat


async def _field(session: AsyncSession) -> Field:
    """Create a field with a unique name (``fields.name`` is UNIQUE)."""
    unique = f"gate-field-{next(_field_counter)}"
    field = Field(name=unique, normalized_name=unique)
    session.add(field)
    await session.commit()
    return field


async def _article(session: AsyncSession, field: Field) -> Main:
    """A review record WITH the qualifying match the resolver reads."""
    category = await _category(session)
    article = Main(
        username="tester",
        title="Thesis",
        abstract="Abstract",
        field="پزشکی",
        university="Tehran University",
        author="Author",
        supervisor="Supervisor",
        status="review",
        field_id=field.id,
        category_id=category.id,
        output_json=dict(OUTPUT),
    )
    session.add(article)
    await session.flush()
    session.add(MainCategoryMatch(
        main_id=article.id, category_id=category.id, score=0.9, rank=1, reason="test",
    ))
    await session.commit()
    return article


@pytest.mark.asyncio
async def test_gate_on_queues_awaiting_confirmation(
    session: AsyncSession, monkeypatch
):
    """Default/gated mode must not create sendable rows."""
    monkeypatch.setattr(settings, "require_human_confirmation", True)
    field = await _field(session)
    channel = Channel(platform="bale", chat_id=9001, username="@c")
    session.add(channel)
    await session.commit()
    # Routing is by CATEGORY now, so a field->channel row would be inert and
    # the gate would have nothing to act on. Point the field's category at the
    # channel instead -- which is what the panel's per-category picker writes.
    await route_category(session, await _category(session), [channel])
    article = await _article(session, field)

    assert await ensure_pending_deliveries(session) == 1
    delivery = (await session.execute(select(Delivery))).scalars().one()
    assert delivery.main_id == article.id
    assert delivery.status == "awaiting_confirmation"
    # Gated rows are never claimable by the sender.
    assert await claim_next_delivery(session) is None


@pytest.mark.asyncio
async def test_gate_off_queues_pending_directly(
    session: AsyncSession, monkeypatch
):
    """Opt-out mode preserves the exact pre-gate behaviour."""
    monkeypatch.setattr(settings, "require_human_confirmation", False)
    field = await _field(session)
    channel = Channel(platform="bale", chat_id=9002, username="@c")
    session.add(channel)
    await session.commit()
    # Routing is by CATEGORY now, so a field->channel row would be inert and
    # the gate would have nothing to act on. Point the field's category at the
    # channel instead -- which is what the panel's per-category picker writes.
    await route_category(session, await _category(session), [channel])
    await _article(session, field)

    assert await ensure_pending_deliveries(session) == 1
    delivery = (await session.execute(select(Delivery))).scalars().one()
    assert delivery.status == "pending"
    assert delivery.attempts == 0


@pytest.mark.asyncio
async def test_confirm_flips_only_gated_rows(
    session: AsyncSession, monkeypatch
):
    """Approval moves awaiting_confirmation to pending, nothing else."""
    monkeypatch.setattr(settings, "require_human_confirmation", True)
    field = await _field(session)
    first = Channel(platform="bale", chat_id=9003, username="@c")
    second = Channel(platform="telegram", chat_id=9004, username="@c")
    session.add_all([first, second])
    await session.commit()
    # Category routing (see the note in the sibling test): the field's category
    # names both channels, which is what makes two deliveries to appear.
    await route_category(
        session, await _category(session), [first, second]
    )
    article = await _article(session, field)
    assert await ensure_pending_deliveries(session) == 2

    # One channel already sent: confirm must leave it alone.
    gated = (
        await session.execute(
            select(Delivery).where(Delivery.channel_id == second.id)
        )
    ).scalar_one()
    gated.status = "sent"
    await session.commit()

    assert await confirm_deliveries_for_article(session, article.id) == 1
    rows = (
        (await session.execute(select(Delivery).order_by(Delivery.channel_id)))
        .scalars()
        .all()
    )
    assert [r.status for r in rows] == ["pending", "sent"]
    # Second approval is a no-op.
    assert await confirm_deliveries_for_article(session, article.id) == 0
