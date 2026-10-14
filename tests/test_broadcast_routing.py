"""Every active channel receives the news; two assignments configure the rest (item 6).

The owner's core rule: "when I write a thesis and the model generates the
news, the news should be sent to all channels". The previous chain
(category route, else field route, else all active) meant that adding ONE
category->channel mapping silently stopped delivery to every other channel.

These tests pin the new rule and the two configuration surfaces that replaced
the route matrices:

* field -> category, stored once and editable by hand (also item 15);
* category -> its designated channel, which is a LABEL and a provenance
  marker, not a filter;
* a channel is kept out of delivery only by switching it OFF.
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
from app.services import channels as channel_service
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
# the rule itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_active_channel_is_a_target(session: AsyncSession):
    category, channels = await _world(session)
    article = await _article(session, category, (await session.get(Field, 1)))
    routed = await channels_for_article(session, article)
    assert [c.id for c in routed] == [c.id for c in channels]


@pytest.mark.asyncio
async def test_a_category_mapping_does_NOT_narrow_delivery(session: AsyncSession):
    """The old bug: one category route silently excluded every other channel."""
    category, channels = await _world(session)
    article = await _article(session, category, (await session.get(Field, 1)))
    # Map the category to exactly ONE channel.
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=channels[0].id))
    await session.commit()
    routed = await channels_for_article(session, article)
    assert len(routed) == 3, (
        "a category->channel label must not stop delivery to the other channels"
    )


@pytest.mark.asyncio
async def test_a_legacy_field_route_does_NOT_narrow_delivery_either(session: AsyncSession):
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    # Old rows may still exist from before the change; they are now inert.
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channels[0].id))
    await session.commit()
    assert len(await channels_for_article(session, article)) == 3


@pytest.mark.asyncio
async def test_switching_a_channel_off_is_what_stops_delivery(session: AsyncSession):
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    channels[1].is_active = False
    await session.commit()
    routed = await channels_for_article(session, article)
    assert [c.id for c in routed] == [channels[0].id, channels[2].id]


@pytest.mark.asyncio
async def test_provenance_records_category_versus_broadcast(session: AsyncSession):
    """The row still says WHY a channel was chosen."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=channels[0].id))
    await session.commit()
    pairs = await channels_for_article_with_source(session, article)
    assert dict((c.id, src) for c, src in pairs) == {
        channels[0].id: "category",
        channels[1].id: "all",
        channels[2].id: "all",
    }


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
# assignment 1: field -> category, stored once (also item 15)
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
    # It persists: re-reading gives the same answer without re-running rules.
    rows = await field_service.field_category_rows(session)
    stored = [r for r in rows if r["id"] == field.id][0]
    assert stored["category_id"] == other.id
    assert stored["category_name"] == "فنی و مهندسی"
    assert stored["category_source"] == "manual"


@pytest.mark.asyncio
async def test_assigning_an_unknown_category_is_refused(session: AsyncSession):
    await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    with pytest.raises(ValueError) as excinfo:
        await field_service.assign_field_category(session, field, 999999)
    assert str(excinfo.value) == "unknown-category"


@pytest.mark.asyncio
async def test_field_rows_list_every_field_with_its_category(session: AsyncSession):
    await _world(session)
    rows = await field_service.field_category_rows(session)
    assert len(rows) == 1
    assert rows[0]["name"] == "test-field"
    assert rows[0]["category_name"] == "پزشکی و علوم سلامت"


# --------------------------------------------------------------------------
# assignment 2: category -> designated channel (a label, not a filter)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_gets_one_designated_channel(session: AsyncSession):
    category, channels = await _world(session)
    assert await channel_service.set_category_channel(
        session, category_id=category.id, channel_id=channels[1].id
    ) == "set"
    matrix = await channel_service.category_channel_matrix(session)
    assert matrix[0]["channel_id"] == channels[1].id

    # Setting another REPLACES it, rather than accumulating several.
    await channel_service.set_category_channel(session, category_id=category.id,
                                              channel_id=channels[2].id)
    matrix = await channel_service.category_channel_matrix(session)
    assert matrix[0]["channel_id"] == channels[2].id
    routes = (await session.execute(
        select(CategoryChannelRoute).where(CategoryChannelRoute.category_id == category.id)
    )).scalars().all()
    assert len(routes) == 1


@pytest.mark.asyncio
async def test_category_channel_can_be_cleared(session: AsyncSession):
    category, channels = await _world(session)
    await channel_service.set_category_channel(session, category_id=category.id,
                                              channel_id=channels[0].id)
    assert await channel_service.set_category_channel(
        session, category_id=category.id, channel_id=None
    ) == "cleared"
    matrix = await channel_service.category_channel_matrix(session)
    assert matrix[0]["channel_id"] is None


@pytest.mark.asyncio
async def test_designated_channel_does_not_filter_delivery(session: AsyncSession):
    """A category's designated channel is a label; everyone still gets the news."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    await channel_service.set_category_channel(session, category_id=category.id,
                                              channel_id=channels[0].id)
    assert len(await channels_for_article(session, article)) == 3


@pytest.mark.asyncio
async def test_unknown_rows_are_refused(session: AsyncSession):
    _, channels = await _world(session)
    with pytest.raises(ValueError) as excinfo:
        await channel_service.set_category_channel(session, category_id=999999,
                                                  channel_id=channels[0].id)
    assert str(excinfo.value) == "unknown-category"
    category = (await session.execute(select(Category))).scalars().one()
    with pytest.raises(ValueError) as excinfo:
        await channel_service.set_category_channel(session, category_id=category.id,
                                                  channel_id=999999)
    assert str(excinfo.value) == "unknown-channel"


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channels_page_shows_the_two_assignments_not_route_matrices(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/channels")
    assert page.status_code == 200
    assert "رشته ← دسته" in page.text
    assert "دسته ← کانال متناظر" in page.text
    assert "/channels/category-channel" in page.text
    assert "/fields/" in page.text
    # The old route matrices and their forms are gone.
    assert "مسیر رشته ← کانال" not in page.text
    assert "مسیر دسته ← کانال" not in page.text
    assert 'action="/channels/route"' not in page.text
    assert 'action="/channels/unroute"' not in page.text
    assert 'action="/channels/category-route"' not in page.text
    assert 'action="/channels/category-unroute"' not in page.text


@pytest.mark.asyncio
async def test_page_explains_that_all_channels_receive_the_news(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/channels")
    assert "همهٔ کانال‌های فعال" in page.text
    assert "ارسال را محدود نمی‌کند" in page.text


@pytest.mark.asyncio
async def test_setting_a_designated_channel_over_http(session: AsyncSession):
    await _admin(session)
    category, channels = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            "/channels/category-channel",
            data={"category_id": str(category.id), "channel_id": str(channels[2].id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
    matrix = await channel_service.category_channel_matrix(session)
    assert matrix[0]["channel_id"] == channels[2].id


@pytest.mark.asyncio
async def test_assigning_a_field_category_over_http(session: AsyncSession):
    await _admin(session)
    await _world(session)
    other = Category(name="فنی و مهندسی", description="Engineering")
    session.add(other)
    await session.commit()
    field = (await session.execute(select(Field))).scalars().one()
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/fields/{field.id}/category", data={"category_id": str(other.id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
    await session.refresh(field)
    assert field.category_id == other.id
    assert field.category_source == "manual"


@pytest.mark.asyncio
async def test_viewer_cannot_change_the_assignments(session: AsyncSession):
    role = (await session.execute(select(Role).where(Role.name == "viewer"))).scalar_one_or_none()
    if role is None:
        role = Role(name="viewer", description="viewer")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="route-viewer", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()
    category, channels = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-viewer", "password": "secret123"},
                          follow_redirects=False)
        denied = await client.post(
            "/channels/category-channel",
            data={"category_id": str(category.id), "channel_id": str(channels[0].id)},
            follow_redirects=False,
        )
        assert denied.status_code == 403
    matrix = await channel_service.category_channel_matrix(session)
    assert matrix[0]["channel_id"] is None
