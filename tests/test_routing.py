"""Delivery targets: the record's QUALIFYING category matches, and only those.

The rule, in one sentence: **a record publishes to exactly the channels its
stored category matches name, and to nothing else.**

Matches (``main_category_matches``) qualify when the category is live
(``deprecated = FALSE``), is not the ``other`` fallback, and carries a score
at or above ``MIN_CATEGORY_RELEVANCE``.

Two consequences that are easy to get wrong, so they are pinned here explicitly:

* a category with NO selected channels publishes nothing. There is no fallback
  to "every active channel", because a fallback would make the panel's mapping
  a decoration: the owner would tick channels, watch the UI change, and see no
  difference in where news goes.
* a record with no qualifying match (nothing stored, or only ``other``) goes
  nowhere, for the same reason — and never auto-publishes.

There is no field-level routing fallback: the category -> channel matrix is
the only path, pinned in ``test_broadcast_routing.py``.

Single-category routing lives here; the multi-category union behaves in
``tests/test_combined_classification.py``.
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
    Main,
    MainCategoryMatch,
)
from app.messaging.dispatcher import (
    channels_for_article_multi,
    ensure_pending_deliveries,
)

OUTPUT = {"title": "عنوان", "lead": "لید", "body": "متن کامل"}

_channel_counter = itertools.count(6000)


async def _category(session: AsyncSession, name: str = "بهداشت") -> Category:
    cat = (
        await session.execute(select(Category).where(Category.name == name))
    ).scalar_one_or_none()
    if cat is None:
        cat = Category(name=name, description=name)
        session.add(cat)
        await session.commit()
    return cat


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


async def _article(
    session: AsyncSession,
    cat: Category,
    *,
    score: float = 0.9,
    matches: list[tuple[Category, float]] | None = None,
) -> Main:
    """Create a review-state article and store its category matches.

    ``matches`` (default ``[(cat, score)]``) is a list of
    ``(category, score)`` stored in score order with rank 1..N, exactly what
    the Worker's post-processing would have written.
    """
    if matches is None:
        matches = [(cat, score)]
    article = Main(
        username="tester",
        title="Thesis",
        abstract="Abstract",
        field="پزشکی",
        university="Tehran University",
        author="Author",
        supervisor="Supervisor",
        status="review",
        field_id=None,
        category_id=matches[0][0].id,
        output_json=dict(OUTPUT),
    )
    session.add(article)
    await session.flush()
    ordered = sorted(matches, key=lambda m: -m[1])
    for rank, (category, value) in enumerate(ordered, start=1):
        session.add(MainCategoryMatch(
            main_id=article.id,
            category_id=category.id,
            score=value,
            rank=rank,
            reason="test",
        ))
    await session.commit()
    return article


# --------------------------------------------------------------------------
# the rule itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_category_channels_are_targets(session: AsyncSession):
    cat = await _category(session)
    routed = await _channel(session)
    await _channel(session)
    await _route(session, cat, routed)
    article = await _article(session, cat)
    assert [c.id for c, _source in await channels_for_article_multi(session, article)] == [routed.id]


@pytest.mark.asyncio
async def test_the_selection_replaces_rather_than_adds(session: AsyncSession):
    """Ticking three and then unticking two must actually stop them."""
    cat = await _category(session)
    first, second, third = await _channel(session), await _channel(session), await _channel(session)
    await _route(session, cat, first, second, third)
    article = await _article(session, cat)
    assert len(await channels_for_article_multi(session, article)) == 3

    await _route(session, cat, first)
    assert [c.id for c, _s in await channels_for_article_multi(session, article)] == [first.id]


@pytest.mark.asyncio
async def test_a_category_with_no_channels_goes_nowhere(session: AsyncSession):
    """The strict half. No fallback to every active channel."""
    cat = await _category(session)
    await _channel(session)
    await _channel(session)
    article = await _article(session, cat)
    assert await channels_for_article_multi(session, article) == []


@pytest.mark.asyncio
async def test_clearing_the_last_channel_stops_delivery(session: AsyncSession):
    cat = await _category(session)
    only = await _channel(session)
    await _route(session, cat, only)
    article = await _article(session, cat)
    assert len(await channels_for_article_multi(session, article)) == 1

    await _route(session, cat)  # untick everything
    assert await channels_for_article_multi(session, article) == []


@pytest.mark.asyncio
async def test_a_record_with_no_category_match_goes_nowhere(session: AsyncSession):
    """No qualifying match (or none at all) means nothing was routed."""
    await _channel(session)
    article = Main(
        username="tester", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        category_id=None, output_json=dict(OUTPUT),
    )
    session.add(article)
    await session.commit()
    assert await channels_for_article_multi(session, article) == []


@pytest.mark.asyncio
async def test_an_other_fallback_only_record_goes_nowhere(session: AsyncSession):
    """A record classified only as ``other`` never auto-publishes."""
    await _channel(session)  # active, but nothing routes to it
    other = await _category(session, "other")
    article = await _article(session, other)
    assert await channels_for_article_multi(session, article) == []
    assert await ensure_pending_deliveries(session) == 0


@pytest.mark.asyncio
async def test_two_categories_reach_different_channels(session: AsyncSession):
    """Routing is per category, which is the whole point of selecting by it."""
    medical = await _category(session, "بهداشت")
    art = await _category(session, "هنر")
    shared = await _channel(session)
    med_only = await _channel(session)
    art_only = await _channel(session)
    await _route(session, medical, shared, med_only)
    await _route(session, art, shared, art_only)

    med_article = await _article(session, medical)
    art_article = await _article(session, art)

    assert [c.id for c, _s in await channels_for_article_multi(session, med_article)] == [
        shared.id, med_only.id,
    ]
    assert [c.id for c, _s in await channels_for_article_multi(session, art_article)] == [
        shared.id, art_only.id,
    ]


@pytest.mark.asyncio
async def test_an_inactive_channel_is_not_a_target_even_when_routed(session: AsyncSession):
    """Switching a channel off removes it from every category at once."""
    cat = await _category(session)
    on = await _channel(session)
    off = await _channel(session, active=False)
    await _route(session, cat, on, off)
    article = await _article(session, cat)
    assert [c.id for c, _s in await channels_for_article_multi(session, article)] == [on.id]


@pytest.mark.asyncio
async def test_provenance_records_the_category_that_decided(session: AsyncSession):
    """So the panel can say WHY a channel received this, not just which."""
    cat = await _category(session)
    channel = await _channel(session)
    await _route(session, cat, channel)
    article = await _article(session, cat)
    pairs = await channels_for_article_multi(session, article)
    assert [(c.id, source) for c, source in pairs] == [(channel.id, cat.name)]


@pytest.mark.asyncio
async def test_an_unrouted_article_is_not_sent_at_all(session: AsyncSession):
    """No destination means no delivery row, not a delivery row with no channel."""
    cat = await _category(session)
    await _channel(session)  # active, but nothing routes to it
    await _article(session, cat)
    assert await ensure_pending_deliveries(session) == 0
    assert (await session.execute(select(Delivery))).scalars().all() == []


# --------------------------------------------------------------------------
# the batch window
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_queues_one_delivery_per_routed_channel(session: AsyncSession):
    cat = await _category(session)
    first, second = await _channel(session), await _channel(session)
    unrouted = await _channel(session)
    await _route(session, cat, first, second)
    await _article(session, cat)

    assert await ensure_pending_deliveries(session) == 2
    rows = (await session.execute(select(Delivery).order_by(Delivery.channel_id))).scalars().all()
    assert [r.channel_id for r in rows] == [first.id, second.id]
    assert all(r.route_source == "category" for r in rows)
    assert unrouted.id not in [r.channel_id for r in rows]
    # Auditable provenance: category_id snapshots the top-1 qualifying match.
    assert all(r.category_id == cat.id for r in rows)


@pytest.mark.asyncio
async def test_ensure_is_idempotent(session: AsyncSession):
    cat = await _category(session)
    await _route(session, cat, await _channel(session))
    await _article(session, cat)
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
    article = await _article(session, cat)
    late = await _channel(session)
    await _route(session, cat, late)

    # The route exists but the channel was activated after the article.
    assert await channels_for_article_multi(session, article) == []
    assert await ensure_pending_deliveries(session) == 0
