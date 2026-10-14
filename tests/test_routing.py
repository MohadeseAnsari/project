"""Delivery targets: EVERY active channel (item 6).

This file used to pin a three-level chain — category routes, then field
routes, then a fallback to all active channels. The owner replaced that with
one rule: the generated news goes to all channels. The two assignment tables
survive as configuration (which category a field belongs to, which channel a
category is designated for), but neither narrows delivery any more.

The old tests asserted exactly the behaviour that was removed (a category
route excluding other channels), so they are replaced rather than deleted.
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
    FieldChannelRoute,
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


@pytest.mark.asyncio
async def test_every_active_channel_receives_the_news(session: AsyncSession):
    cat = await _category(session)
    field = await _field(session, cat)
    first = await _channel(session)
    second = await _channel(session, "telegram")
    article = await _article(session, field, cat)

    assert [c.id for c in await channels_for_article(session, article)] == [first.id, second.id]
    assert await channels_for_article_with_source(session, article) == [
        (first, "all"),
        (second, "all"),
    ]


@pytest.mark.asyncio
async def test_a_category_mapping_no_longer_excludes_other_channels(session: AsyncSession):
    """The behaviour that was wrong before: one mapping used to filter delivery."""
    cat = await _category(session)
    field = await _field(session, cat)
    designated = await _channel(session)
    other = await _channel(session, "telegram")
    session.add(CategoryChannelRoute(category_id=cat.id, channel_id=designated.id))
    await session.commit()
    article = await _article(session, field, cat)

    assert [c.id for c in await channels_for_article(session, article)] == [designated.id, other.id]
    assert await channels_for_article_with_source(session, article) == [
        (designated, "category"),
        (other, "all"),
    ], "the mapping still records WHY a channel was chosen"


@pytest.mark.asyncio
async def test_legacy_field_routes_are_inert(session: AsyncSession):
    """Rows from before the change still exist and must no longer narrow delivery."""
    cat = await _category(session)
    field = await _field(session, cat)
    mapped = await _channel(session)
    await _channel(session, "telegram")
    session.add(FieldChannelRoute(field_id=field.id, channel_id=mapped.id))
    await session.commit()
    article = await _article(session, field, cat)

    assert len(await channels_for_article(session, article)) == 2


@pytest.mark.asyncio
async def test_an_inactive_channel_is_the_only_way_to_exclude_one(session: AsyncSession):
    cat = await _category(session)
    field = await _field(session, cat)
    on = await _channel(session)
    off = await _channel(session, "telegram", active=False)
    article = await _article(session, field, cat)

    assert [c.id for c in await channels_for_article(session, article)] == [on.id]
    assert off.id not in [c.id for c in await channels_for_article(session, article)]


@pytest.mark.asyncio
async def test_no_active_channels_resolves_to_nothing(session: AsyncSession):
    cat = await _category(session)
    field = await _field(session, cat)
    await _channel(session, active=False)
    article = await _article(session, field, cat)

    assert await channels_for_article(session, article) == []


@pytest.mark.asyncio
async def test_ensure_queues_a_delivery_per_active_channel(session: AsyncSession, monkeypatch):
    """One article, one delivery row per channel — the fan-out, not a route."""
    from app.config import settings

    monkeypatch.setattr(settings, "require_human_confirmation", False)
    cat = await _category(session)
    field = await _field(session, cat)
    await _channel(session)
    await _channel(session, "telegram")
    await _channel(session, "bale", active=False)
    article = await _article(session, field, cat)

    assert await ensure_pending_deliveries(session) == 2
    rows = (await session.execute(select(Delivery))).scalars().all()
    assert {row.route_source for row in rows} == {"all"}
    assert {row.category_id for row in rows} == {cat.id}


@pytest.mark.asyncio
async def test_ensure_records_provenance_for_the_designated_channel(
    session: AsyncSession, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "require_human_confirmation", False)
    cat = await _category(session)
    field = await _field(session, cat)
    designated = await _channel(session)
    other = await _channel(session, "telegram")
    session.add(CategoryChannelRoute(category_id=cat.id, channel_id=designated.id))
    await session.commit()
    article = await _article(session, field, cat)

    assert await ensure_pending_deliveries(session) == 2
    rows = (await session.execute(select(Delivery).order_by(Delivery.channel_id))).scalars().all()
    sources = {row.channel_id: row.route_source for row in rows}
    assert sources[designated.id] == "category"
    assert sources[other.id] == "all"
    assert all(row.category_id == cat.id for row in rows)


@pytest.mark.asyncio
async def test_a_channel_enabled_later_gets_its_own_delivery(session: AsyncSession, monkeypatch):
    """A newly switched-on channel is picked up for an already-approved article."""
    from app.config import settings

    monkeypatch.setattr(settings, "require_human_confirmation", False)
    cat = await _category(session)
    field = await _field(session, cat)
    first = await _channel(session)
    late = await _channel(session, "telegram", active=False)
    article = await _article(session, field, cat)

    assert await ensure_pending_deliveries(session) == 1
    late.is_active = True
    await session.commit()

    assert await ensure_pending_deliveries(session) == 1, "only the new channel is queued"
    rows = (await session.execute(select(Delivery).order_by(Delivery.id))).scalars().all()
    assert [row.channel_id for row in rows] == [first.id, late.id]
