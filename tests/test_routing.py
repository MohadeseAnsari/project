"""Delivery targets: the record's CATEGORY, and only that.

The rule, in one sentence: **a record publishes to exactly the channels its
category names, and to nothing else.**

Two consequences that are easy to get wrong, so they are pinned here explicitly:

* a category with NO selected channels publishes nothing. There is no fallback
  to "every active channel", because a fallback would make the panel's mapping
  a decoration: the owner would tick channels, watch the UI change, and see no
  difference in where news goes.
* a record with no category at all goes nowhere, for the same reason.

What this file deliberately does NOT assert is that routing is the *only*
thing that decides delivery. ``main.field_channel_routes`` still exists and is
still ignored; that is pinned in ``test_broadcast_routing.py``.
"""

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Category,
    CategoryChannelRoute,
    Channel,
    Delivery,
    Field,
    Main,
)
from app.messaging.dispatcher import (
    channels_for_article,
    channels_for_article_with_source,
    ensure_pending_deliveries,
)

OUTPUT = {"title": "عنوان", "lead": "لید", "body": "متن کامل"}

_field_counter = itertools.count(5000)
_channel_counter = itertools.count(6000)


async def _category(session: AsyncSession, name: str = "پزشکی و علوم سلامت") -> Category:
    cat = (
        await session.execute(select(Category).where(Category.name == name))
    ).scalar_one_or_none()
    if cat is None:
        cat = Category(name=name, description=name)
        session.add(cat)
        await session.commit()
    return cat


async def _field(session: AsyncSession, cat: Category) -> Field:
    unique = f"route-field-{next(_field_counter)}"
    field = Field(name=unique, normalized_name=unique, category_id=cat.id)
    session.add(field)
    await session.commit()
    return field


async def _channel(session: AsyncSession, platform: str = "bale", active: bool = True) -> Channel:
    chat_id = next(_channel_counter)
    channel = Channel(platform=platform, chat_id=chat_id, username=f"@c{chat_id}", is_active=active)
    session.add(channel)
    await session.commit()
    return channel


async def _route(session: AsyncSession, cat: Category, *channels: Channel) -> None:
    """Point ``cat`` at exactly these channels, replacing anything else."""
    await session.execute(
        CategoryChannelRoute.__table__.delete().where(
            CategoryChannelRoute.category_id == cat.id
        )
    )
    for channel in channels:
        session.add(CategoryChannelRoute(category_id=cat.id, channel_id=channel.id))
    await session.commit()


async def _article(session: AsyncSession, field: Field, cat: Category) -> Main:
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
        category_id=cat.id,
        output_json=dict(OUTPUT),
    )
    session.add(article)
    await session.commit()
    return article


# --------------------------------------------------------------------------
# the rule itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_category_channels_are_targets(session: AsyncSession):
    cat = await _category(session)
    field = await _field(session, cat)
    routed = await _channel(session)
    unrouted = await _channel(session)
    await _route(session, cat, routed)
    article = await _article(session, field, cat)
    assert [c.id for c in await channels_for_article(session, article)] == [routed.id]


@pytest.mark.asyncio
async def test_the_selection_replaces_rather_than_adds(session: AsyncSession):
    """Ticking three and then unticking one must actually stop that one."""
    cat = await _category(session)
    field = await _field(session, cat)
    first, second, third = await _channel(session), await _channel(session), await _channel(session)
    await _route(session, cat, first, second, third)
    article = await _article(session, field, cat)
    assert len(await channels_for_article(session, article)) == 3

    await _route(session, cat, first)
    assert [c.id for c in await channels_for_article(session, article)] == [first.id]


@pytest.mark.asyncio
async def test_a_category_with_no_channels_goes_nowhere(session: AsyncSession):
    """The strict half. No fallback to every active channel."""
    cat = await _category(session)
    field = await _field(session, cat)
    await _channel(session)
    await _channel(session)
    article = await _article(session, field, cat)
    assert await channels_for_article(session, article) == []


@pytest.mark.asyncio
async def test_clearing_the_last_channel_stops_delivery(session: AsyncSession):
    cat = await _category(session)
    field = await _field(session, cat)
    only = await _channel(session)
    await _route(session, cat, only)
    article = await _article(session, field, cat)
    assert len(await channels_for_article(session, article)) == 1

    await _route(session, cat)  # untick everything
    assert await channels_for_article(session, article) == []


@pytest.mark.asyncio
async def test_a_record_with_no_category_goes_nowhere(session: AsyncSession):
    """No category means no route was ever chosen for it."""
    field_row = await _field(session, await _category(session))
    await _channel(session)
    article = Main(
        username="tester", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review", field_id=field_row.id,
        category_id=None, output_json=dict(OUTPUT),
    )
    session.add(article)
    await session.commit()
    assert await channels_for_article(session, article) == []


@pytest.mark.asyncio
async def test_two_categories_reach_different_channels(session: AsyncSession):
    """Routing is per category, which is the whole point of selecting by it."""
    medical = await _category(session, "پزشکی و علوم سلامت")
    art = await _category(session, "هنر و معماری")
    shared = await _channel(session)
    med_only = await _channel(session)
    art_only = await _channel(session)
    await _route(session, medical, shared, med_only)
    await _route(session, art, shared, art_only)

    med_field = await _field(session, medical)
    art_field = await _field(session, art)
    med_article = await _article(session, med_field, medical)
    art_article = await _article(session, art_field, art)

    assert [c.id for c in await channels_for_article(session, med_article)] == [
        shared.id, med_only.id,
    ]
    assert [c.id for c in await channels_for_article(session, art_article)] == [
        shared.id, art_only.id,
    ]


@pytest.mark.asyncio
async def test_an_inactive_channel_is_not_a_target_even_when_routed(session: AsyncSession):
    """Switching a channel off removes it from every category at once."""
    cat = await _category(session)
    field = await _field(session, cat)
    on = await _channel(session)
    off = await _channel(session, active=False)
    await _route(session, cat, on, off)
    article = await _article(session, field, cat)
    assert [c.id for c in await channels_for_article(session, article)] == [on.id]


@pytest.mark.asyncio
async def test_provenance_records_the_category_that_decided(session: AsyncSession):
    """So the panel can say WHY a channel received this, not just which."""
    cat = await _category(session)
    field = await _field(session, cat)
    channel = await _channel(session)
    await _route(session, cat, channel)
    article = await _article(session, field, cat)
    pairs = await channels_for_article_with_source(session, article)
    assert [(c.id, source) for c, source in pairs] == [(channel.id, "category")]


@pytest.mark.asyncio
async def test_an_unrouted_article_is_not_sent_at_all(session: AsyncSession):
    """No destination means no delivery row, not a delivery row with no channel."""
    cat = await _category(session)
    field = await _field(session, cat)
    await _channel(session)  # active, but nothing routes to it
    article = await _article(session, field, cat)
    assert await ensure_pending_deliveries(session) == 0
    assert (await session.execute(select(Delivery))).scalars().all() == []


# --------------------------------------------------------------------------
# the batch window
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_queues_one_delivery_per_routed_channel(session: AsyncSession):
    cat = await _category(session)
    field = await _field(session, cat)
    first, second = await _channel(session), await _channel(session)
    unrouted = await _channel(session)
    await _route(session, cat, first, second)
    await _article(session, field, cat)

    assert await ensure_pending_deliveries(session) == 2
    rows = (await session.execute(select(Delivery).order_by(Delivery.channel_id))).scalars().all()
    assert [r.channel_id for r in rows] == [first.id, second.id]
    assert all(r.route_source == "category" for r in rows)
    assert unrouted.id not in [r.channel_id for r in rows]


@pytest.mark.asyncio
async def test_ensure_is_idempotent(session: AsyncSession):
    cat = await _category(session)
    field = await _field(session, cat)
    await _route(session, cat, await _channel(session))
    await _article(session, field, cat)
    assert await ensure_pending_deliveries(session) == 1
    assert await ensure_pending_deliveries(session) == 0


@pytest.mark.asyncio
async def test_routes_added_later_do_not_backfill_the_archive(session: AsyncSession):
    """The activation floor: news generated before a channel existed, or before
    the category pointed at it, is not replayed into it.

    This is the failure mode that makes routing dangerous to switch on: without
    the floor, ticking a channel would make the sender enqueue every historical
    article, and the owner would wake up to a channel full of old news.
    """
    cat = await _category(session)
    field = await _field(session, cat)
    article = await _article(session, field, cat)
    late = await _channel(session)
    await _route(session, cat, late)

    # The route exists but the channel was activated after the article.
    assert await channels_for_article(session, article) == []
    assert await ensure_pending_deliveries(session) == 0