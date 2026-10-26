"""A channel must not inherit the news that already existed before it existed.

The bug, in the owner's words: *"when i add a new channel with its bot you send
all records news again in the every channel created first i dont want this ...
from now on whenever i created a new thesis to be generated news by model for
that send the news of it simultaneously with other channels."*

The cause was in :func:`ensure_pending_deliveries`: it asked "does this article
have an ACTIVE channel with no delivery row yet?" A brand new channel has no
delivery rows for anything, ever, so every historical record matched and the
sender built the entire backlog for it on every pass.

The rule these tests pin:

    an article is a target for a channel only if the channel was switched on
    no later than the article

which is enforced in two places that MUST agree -- ``channels_for_article_multi``
in Python and ``_has_undelivered_channel`` in SQL. A disagreement in either
direction is a silent bug: skip too much and news never goes out, skip too
little and the archive floods a new channel. Most of this file exists to keep
those two in step.
"""

import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Channel, Delivery, Field, Main, MainCategoryMatch
from app.messaging.dispatcher import (
    channels_for_article_multi,
    ensure_pending_deliveries,
)
from tests.routing_setup import route_all_active

OUTPUT = {"title": "تیتر", "lead": "lead", "body": "body"}


async def _field(session: AsyncSession) -> Field:
    field = Field(name="test-field", normalized_name="test-field")
    session.add(field)
    await session.commit()
    return field


_CHAT_SEQ = itertools.count(95000)


async def _channel(session: AsyncSession, activated_at: datetime, active: bool = True) -> Channel:
    channel = Channel(
        platform="bale", chat_id=next(_CHAT_SEQ),
        username="@c", title="C", is_active=active, activated_at=activated_at,
    )
    session.add(channel)
    await session.commit()
    await _route_to_all(session)
    return channel


async def _article(session: AsyncSession, field: Field, created_at: datetime,
                   status: str = "review") -> Main:
    """A record WITH the qualifying category match that routing reads."""
    category = (await session.execute(select(Category).order_by(Category.id))).scalars().first()
    if category is None:
        category = Category(name="پزشکی و علوم سلامت", description="Medical")
        session.add(category)
        await session.commit()
    article = Main(
        username="u", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status=status, field_id=field.id,
        category_id=category.id,
        output_json=OUTPUT, created_at=created_at,
    )
    session.add(article)
    await session.flush()
    session.add(MainCategoryMatch(
        main_id=article.id, category_id=category.id, score=0.9, rank=1, reason="test",
    ))
    await session.commit()
    await _route_to_all(session)
    return article


async def _route_to_all(session: AsyncSession) -> None:
    """Every category reaches every active channel.

    Re-seeded after each channel AND each article is created so call order
    does not matter. The activation floor is untouched: a channel switched on
    AFTER an article still must not receive it, and these tests assert exactly
    that.
    """
    categories = (await session.execute(select(Category))).scalars().all()
    for category in categories:
        await route_all_active(session, category)


NOW = datetime(2026, 3, 1, 12, 0, 0)


# --------------------------------------------------------------------------
# the rule itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_channel_does_not_receive_news_that_predates_it(session: AsyncSession):
    field = await _field(session)
    old = await _article(session, field, NOW)
    await _channel(session, NOW + timedelta(days=1))
    assert [c.id for c, _s in await channels_for_article_multi(session, old)] == [], (
        "the archive must not be delivered to a channel added afterwards"
    )


@pytest.mark.asyncio
async def test_a_channel_receives_news_created_after_it_was_switched_on(session: AsyncSession):
    field = await _field(session)
    channel = await _channel(session, NOW)
    later = await _article(session, field, NOW + timedelta(minutes=1))
    assert [c.id for c, _s in await channels_for_article_multi(session, later)] == [channel.id]


@pytest.mark.asyncio
async def test_the_boundary_minute_itself_is_included(session: AsyncSession):
    """Same timestamp, not strictly after.

    PostgreSQL's now() is the transaction start time, so a channel and an
    article created back to back can legitimately share a timestamp. Using a
    strict inequality would drop that article forever with nothing to show for
    it, which is the worse failure.
    """
    field = await _field(session)
    channel = await _channel(session, NOW)
    same = await _article(session, field, NOW)
    assert [c.id for c, _s in await channels_for_article_multi(session, same)] == [channel.id]


@pytest.mark.asyncio
async def test_an_old_channel_still_receives_new_news(session: AsyncSession):
    """The floor must not become a ceiling that silences working channels."""
    field = await _field(session)
    old_channel = await _channel(session, NOW - timedelta(days=365))
    fresh = await _article(session, field, NOW)
    assert [c.id for c, _s in await channels_for_article_multi(session, fresh)] == [old_channel.id]


@pytest.mark.asyncio
async def test_a_channel_only_sees_the_news_of_its_own_era(session: AsyncSession):
    """Two channels, two archives, no cross-contamination."""
    field = await _field(session)
    early = await _article(session, field, NOW - timedelta(days=1))
    before_switch = await _channel(session, NOW - timedelta(days=10))
    after_switch = await _article(session, field, NOW + timedelta(days=10))
    joined_late = await _channel(session, NOW)

    assert [c.id for c, _s in await channels_for_article_multi(session, early)] == [before_switch.id]
    assert [c.id for c, _s in await channels_for_article_multi(session, after_switch)] == [
        before_switch.id, joined_late.id
    ]


@pytest.mark.asyncio
async def test_switching_a_channel_off_and_on_moves_its_start_forward(session: AsyncSession):
    """Re-enabling is the same decision as creating a fresh channel."""
    from app.services import channels as channel_service

    field = await _field(session)
    dormant = await _channel(session, NOW - timedelta(days=365), active=False)
    await channel_service.toggle_channel(session, dormant)
    await session.refresh(dormant)
    assert dormant.is_active is True
    assert dormant.activated_at > NOW - timedelta(days=364), (
        "the boundary must move, or a dormant channel wakes up to the backlog"
    )

    await _route_to_all(session)
    stale = await _article(session, field, NOW + timedelta(days=1))
    fresh = await _article(session, field, datetime.utcnow() + timedelta(seconds=5))
    assert [c.id for c, _s in await channels_for_article_multi(session, stale)] == []
    assert [c.id for c, _s in await channels_for_article_multi(session, fresh)] == [dormant.id]


@pytest.mark.asyncio
async def test_switching_a_channel_off_leaves_its_boundary_alone(session: AsyncSession):
    from app.services import channels as channel_service

    channel = await _channel(session, NOW, active=True)
    before = channel.activated_at
    await channel_service.toggle_channel(session, channel)
    await session.refresh(channel)
    assert channel.is_active is False
    assert channel.activated_at == before, "going dark must not move the boundary"


@pytest.mark.asyncio
async def test_provenance_is_still_category_for_new_news(session: AsyncSession):
    """A single qualifying match names the category that reached the channel."""
    field = await _field(session)
    await _channel(session, NOW - timedelta(days=1))
    article = await _article(session, field, NOW)
    pairs = await channels_for_article_multi(session, article)
    assert all(source == "پزشکی و علوم سلامت" for _c, source in pairs), (
        "one match, one category name as provenance"
    )


# --------------------------------------------------------------------------
# the batch query must agree with the resolver
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_queue_does_not_backfill_a_new_channel(session: AsyncSession):
    """The regression itself, at the level the sender actually runs."""
    from app.config import settings

    field = await _field(session)
    old = await _article(session, field, NOW - timedelta(days=10), status="published")
    await _channel(session, NOW)
    before = settings.require_human_confirmation
    settings.require_human_confirmation = False
    try:
        assert await ensure_pending_deliveries(session) == 0, (
            "the archive must not be queued for a channel that postdates it"
        )
    finally:
        settings.require_human_confirmation = before

    new = await _article(session, field, NOW + timedelta(minutes=5))
    assert await ensure_pending_deliveries(session) == 1
    rows = (await session.execute(select(Delivery).order_by(Delivery.main_id))).scalars().all()
    assert [r.main_id for r in rows] == [new.id], "only the new record is queued"
    assert [r.main_id for r in rows] != [old.id]


@pytest.mark.asyncio
async def test_the_queue_and_the_resolver_agree_on_both_sides(session: AsyncSession):
    """Guard against drift between the SQL predicate and the Python resolver.

    Three articles and two channels straddle the boundaries. Whatever the
    resolver says a channel should get, the queue must deliver -- and nothing
    more. If these two ever disagree the symptom is invisible, so it is worth
    asserting them against each other rather than against fixed expectations.
    """
    from app.config import settings

    field = await _field(session)
    early = await _article(session, field, NOW - timedelta(days=30))
    middle = await _article(session, field, NOW - timedelta(days=5))
    late = await _article(session, field, NOW + timedelta(days=5))
    await _channel(session, NOW - timedelta(days=10))
    await _channel(session, NOW)

    before = settings.require_human_confirmation
    settings.require_human_confirmation = False
    try:
        assert await ensure_pending_deliveries(session) >= 3
    finally:
        settings.require_human_confirmation = before

    for article in (early, middle, late):
        expected = sorted(c.id for c, _s in await channels_for_article_multi(session, article))
        actual = sorted(
            row.channel_id
            for row in (
                await session.execute(
                    select(Delivery).where(Delivery.main_id == article.id)
                )
            ).scalars().all()
        )
        assert actual == expected, f"article {article.id}: SQL and Python disagree"


@pytest.mark.asyncio
async def test_an_article_with_nothing_to_do_is_never_selected(session: AsyncSession):
    """The anti-join must not resurface a fully-delivered article forever."""
    field = await _field(session)
    channel = await _channel(session, NOW - timedelta(days=1))
    article = await _article(session, field, NOW)
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="sent",
                         platform_message_id=1))
    await session.commit()
    assert await ensure_pending_deliveries(session) == 0
