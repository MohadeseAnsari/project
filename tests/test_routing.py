"""Tests for the category-first channel routing chain.

Resolution order: category routes → field routes (exceptions) → all active
channels (fallback). Every created delivery snapshots which category and
which route level produced it.
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


async def _channel(session: AsyncSession, platform: str = "bale") -> Channel:
    chat_id = next(_channel_counter)
    channel = Channel(platform=platform, chat_id=chat_id, username=f"@c{chat_id}")
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
async def test_category_routes_win_over_field_routes(session: AsyncSession):
    """A field exception never overrides its own category mapping."""
    cat = await _category(session)
    field = await _field(session, cat)
    cat_channel = await _channel(session)
    field_channel = await _channel(session)
    session.add(CategoryChannelRoute(category_id=cat.id, channel_id=cat_channel.id))
    session.add(FieldChannelRoute(field_id=field.id, channel_id=field_channel.id))
    await session.commit()
    article = await _article(session, field, cat)

    assert [c.id for c in await channels_for_article(session, article)] == [cat_channel.id]
    assert await channels_for_article_with_source(session, article) == [(cat_channel, "category")]


@pytest.mark.asyncio
async def test_field_routes_fire_without_category_route(session: AsyncSession):
    """Field routes stay as the exception level when the category is unmapped."""
    cat = await _category(session)
    field = await _field(session, cat)
    field_channel = await _channel(session)
    await _channel(session)  # unmapped active channel: must NOT receive
    session.add(FieldChannelRoute(field_id=field.id, channel_id=field_channel.id))
    await session.commit()
    article = await _article(session, field, cat)

    assert await channels_for_article_with_source(session, article) == [(field_channel, "field")]


@pytest.mark.asyncio
async def test_fallback_to_all_active_channels(session: AsyncSession):
    """Unmapped categories still publish instead of silently dropping."""
    cat = await _category(session)
    field = await _field(session, cat)
    first = await _channel(session)
    second = await _channel(session, "telegram")
    article = await _article(session, field, cat)

    assert await channels_for_article_with_source(session, article) == [
        (first, "fallback"),
        (second, "fallback"),
    ]


@pytest.mark.asyncio
async def test_ensure_records_routing_provenance(session: AsyncSession, monkeypatch):
    """Queued deliveries snapshot category_id + route_source at queue time."""
    from app.config import settings

    monkeypatch.setattr(settings, "require_human_confirmation", False)
    cat = await _category(session)
    field = await _field(session, cat)
    channel = await _channel(session)
    session.add(CategoryChannelRoute(category_id=cat.id, channel_id=channel.id))
    await session.commit()
    article = await _article(session, field, cat)

    assert await ensure_pending_deliveries(session) == 1
    delivery = (await session.execute(select(Delivery))).scalars().one()
    assert delivery.category_id == cat.id
    assert delivery.route_source == "category"
