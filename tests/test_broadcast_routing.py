"""Every active channel receives the news; there is no routing to configure (item 6).

The owner's final rule, after a round of back and forth: **"I want to send the
generated messages to all channels without choosing which channel to go or
which field to go to which channel."**

So this file pins a single rule plus the one control that actually decides
where news does not go (switching a channel off), and asserts that the routing
configuration screens are genuinely gone rather than merely hidden.

``fields.category_id`` survives: that is the CLASSIFICATION the generator and
the records use, it is not routing, and it lives on the fields page.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Category,
    CategoryChannelRoute,
    Channel,
    Field,
    FieldChannelRoute,
    Main,
    PanelUser,
    Role,
)
from app.messaging.dispatcher import channels_for_article, channels_for_article_with_source
from app.panel.auth import hash_password
from app.services import fields as field_service
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession, username: str = "route-admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _world(session: AsyncSession) -> tuple[Category, list[Channel]]:
    category = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(category)
    await session.commit()
    field = Field(name="test-field", normalized_name="test-field", category_id=category.id)
    session.add(field)
    await session.commit()
    channels = []
    for index, chat_id in enumerate((92001, 92002, 92003)):
        channel = Channel(platform="bale", chat_id=chat_id, username=f"@c{index}", is_active=True)
        session.add(channel)
        await session.commit()
        channels.append(channel)
    return category, channels


async def _article(session: AsyncSession, category: Category, field: Field) -> Main:
    row = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
               author="A", supervisor="S", status="approved", field_id=field.id,
               category_id=category.id,
               output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(row)
    await session.commit()
    return row


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_active_channel_is_a_target(session: AsyncSession):
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    assert [c.id for c in await channels_for_article(session, article)] == [c.id for c in channels]


@pytest.mark.asyncio
async def test_a_legacy_category_mapping_changes_nothing(session: AsyncSession):
    """Rows from before the change still exist and are simply ignored."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=channels[0].id))
    await session.commit()
    assert len(await channels_for_article(session, article)) == 3


@pytest.mark.asyncio
async def test_a_legacy_field_route_changes_nothing(session: AsyncSession):
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channels[0].id))
    await session.commit()
    assert len(await channels_for_article(session, article)) == 3


@pytest.mark.asyncio
async def test_switching_a_channel_off_is_the_only_way_to_exclude_one(session: AsyncSession):
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    channels[1].is_active = False
    await session.commit()
    assert [c.id for c in await channels_for_article(session, article)] == [
        channels[0].id, channels[2].id
    ]


@pytest.mark.asyncio
async def test_provenance_is_always_broadcast(session: AsyncSession):
    """There is one rule, so there is one reason."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=channels[0].id))
    await session.commit()
    pairs = await channels_for_article_with_source(session, article)
    assert {source for _channel, source in pairs} == {"all"}


@pytest.mark.asyncio
async def test_article_without_a_category_still_reaches_every_channel(session: AsyncSession):
    _, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = Main(username="u", title="T", abstract="A", field="ناشناخته", university="U",
                   author="A", supervisor="S", status="approved", field_id=field.id,
                   output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(article)
    await session.commit()
    assert len(await channels_for_article(session, article)) == 3


# --------------------------------------------------------------------------
# classification is NOT routing, and it stays
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_field_category_is_stored_once_and_marked_manual(session: AsyncSession):
    category, _channels = await _world(session)
    other = Category(name="فنی و مهندسی", description="Engineering")
    session.add(other)
    await session.commit()
    field = (await session.execute(select(Field))).scalars().one()

    await field_service.assign_field_category(session, field, other.id)
    await session.refresh(field)
    assert field.category_id == other.id
    assert field.category_source == "manual", (
        "an operator decision must be visible and not look like a ruleset guess"
    )
    rows, _total = await field_service.field_category_rows(session)
    stored = [r for r in rows if r["id"] == field.id][0]
    assert stored["category_id"] == other.id
    assert stored["category_name"] == "فنی و مهندسی"


@pytest.mark.asyncio
async def test_assigning_an_unknown_category_is_refused(session: AsyncSession):
    await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    with pytest.raises(ValueError) as excinfo:
        await field_service.assign_field_category(session, field, 999999)
    assert str(excinfo.value) == "unknown-category"


@pytest.mark.asyncio
async def test_field_category_is_editable_on_the_fields_page(session: AsyncSession):
    """Classification kept its home; only routing left the panel."""
    await _admin(session)
    category, _channels = await _world(session)
    other = Category(name="فنی و مهندسی", description="Engineering")
    session.add(other)
    await session.commit()
    field = (await session.execute(select(Field))).scalars().one()
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields")
        assert f"/fields/{field.id}/category" in page.text
        response = await client.post(
            f"/fields/{field.id}/category", data={"category_id": str(other.id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/fields"
    await session.refresh(field)
    assert field.category_id == other.id


# --------------------------------------------------------------------------
# the routing screens are gone, not just hidden
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_channels_page_has_no_routing_controls(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/channels")
    assert page.status_code == 200
    # Neither assignment survives anywhere on the page.
    assert "رشته ← دسته" not in page.text
    assert "دسته ← کانال متناظر" not in page.text
    assert "/channels/category-channel" not in page.text
    assert "/category" not in page.text, "the field->category editor left too"
    # What IS there: managing destinations.
    assert "کانال‌ها" in page.text
    assert "افزودن کانال" in page.text


def test_the_routing_endpoints_no_longer_exist():
    from app.panel.app import panel_app

    paths = {getattr(r, "path", None) for r in panel_app.routes}
    assert "/channels/category-channel" not in paths
    assert "/channels/table" not in paths


def test_the_channels_service_no_longer_manages_routing():
    from app.services import channels as channel_service

    for gone in ("category_channel_matrix", "set_category_channel",
                 "add_field_route", "add_category_route"):
        assert not hasattr(channel_service, gone), f"{gone} is dead code now"


@pytest.mark.asyncio
async def test_the_fields_page_has_no_channel_mapping_either(session: AsyncSession):
    """'Which channel for this category' must not survive on another page."""
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields")
    assert "دسته‌ها و کانال متناظر" not in page.text
    assert "/channels/category-channel" not in page.text


@pytest.mark.asyncio
async def test_the_page_still_says_where_news_goes(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/channels")
    assert "همهٔ کانال‌های فعال" in page.text
    assert "خاموش کنید" in page.text
