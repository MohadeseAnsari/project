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
    """Routing is configuration-only again: writable, never read by the sender.

    The per-row add helpers stay gone. ``set_category_channels`` REPLACES a
    category's whole set instead, because the panel's checkbox list is the
    complete truth about that category and an "add one row" helper could not
    express "untick this".

    What matters more is what this file exists to pin: filling the matrix in
    must not change where news goes. That is asserted behaviourally in
    ``test_a_filled_category_matrix_still_broadcasts`` below, which is a
    stronger guarantee than checking that a function is absent -- a missing
    function can always be re-added by accident, whereas a full matrix that
    still broadcasts cannot.
    """
    from app.services import channels as channel_service

    for gone in ("set_category_channel", "add_field_route", "add_category_route"):
        assert not hasattr(channel_service, gone), f"{gone} is dead code now"
    assert hasattr(channel_service, "category_channel_matrix")
    assert hasattr(channel_service, "set_category_channels")


@pytest.mark.asyncio
async def test_a_filled_category_matrix_still_broadcasts(session: AsyncSession):
    """The owner's rule, checked against a matrix the operator has filled in.

    The panel now lets a category name its channels. If that ever started
    filtering delivery, this fails -- which is the whole point of writing it
    against real rows rather than asserting a function is absent.
    """
    from app.services import channels as channel_service

    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)

    # Map the category to exactly one channel, the most selective thing the
    # matrix can express.
    await channel_service.set_category_channels(session, category.id, [channels[0].id])
    matrix = await channel_service.category_channel_matrix(session)
    assert [row["channel_ids"] for row in matrix if row["id"] == category.id] == [
        [channels[0].id]
    ]
    # ...and the article still goes everywhere.
    assert [c.id for c in await channels_for_article(session, article)] == [
        c.id for c in channels
    ]


@pytest.mark.asyncio
async def test_replacing_a_category_matrix_removes_the_old_rows(session: AsyncSession):
    """Set, not merge: an unticked channel must actually stop being routed."""
    from app.services import channels as channel_service

    category, channels = await _world(session)
    await channel_service.set_category_channels(session, category.id, [c.id for c in channels])
    await channel_service.set_category_channels(session, category.id, [channels[1].id])
    matrix = await channel_service.category_channel_matrix(session)
    assert [row["channel_ids"] for row in matrix if row["id"] == category.id] == [
        [channels[1].id]
    ]


@pytest.mark.asyncio
async def test_an_empty_selection_clears_a_category(session: AsyncSession):
    category, channels = await _world(session)
    await channel_service.set_category_channels(session, category.id, [c.id for c in channels])
    assert await channel_service.set_category_channels(session, category.id, []) == []
    matrix = await channel_service.category_channel_matrix(session)
    assert [row["channel_ids"] for row in matrix if row["id"] == category.id] == [[]]


@pytest.mark.asyncio
async def test_the_categories_page_offers_the_mapping_and_says_it_is_not_live(
    session: AsyncSession,
):
    """The matrix must never be mistaken for something the sender obeys."""
    await _admin(session)
    category, _channels = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/categories")
    assert page.status_code == 200
    assert f"/categories/{category.id}/channels" in page.text
    assert "فعلاً اعمال نمی‌شود" in page.text, (
        "the page must say the mapping is recorded, not enforced"
    )


@pytest.mark.asyncio
async def test_saving_the_matrix_is_admin_only_and_audited(session: AsyncSession):
    from sqlalchemy import select as _select

    from app.db.models import AuditLog, PanelUser as _PanelUser

    category, channels = await _world(session)
    await _admin(session, username="matrix-admin")
    viewer_role = (await session.execute(
        _select(Role).where(Role.name == "viewer"))).scalar_one_or_none()
    if viewer_role is None:
        viewer_role = Role(name="viewer", description="viewer")
        session.add(viewer_role)
        await session.flush()
    session.add(_PanelUser(username="matrix-viewer", password_hash=hash_password("secret123"),
                           role_id=viewer_role.id, is_active=True))
    await session.commit()

    async with panel_client() as client:
        await client.post("/login", data={"username": "matrix-admin", "password": "secret123"},
                          follow_redirects=False)
        saved = await client.post(
            f"/categories/{category.id}/channels",
            data={"channel_ids": [str(c.id) for c in channels]},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert saved.headers["location"] == "/categories"

        await client.post("/logout")
        await client.post("/login", data={"username": "matrix-viewer", "password": "secret123"},
                          follow_redirects=False)
        refused = await client.post(
            f"/categories/{category.id}/channels", data={"channel_ids": ["1"]},
            follow_redirects=False,
        )
        assert refused.status_code == 403

    matrix = await channel_service.category_channel_matrix(session)
    assert [row["channel_ids"] for row in matrix if row["id"] == category.id] == [
        [c.id for c in channels]
    ], "the viewer's attempt must not have changed anything"
    actions = (await session.execute(
        _select(AuditLog.action).where(AuditLog.action == "category_channels")
    )).scalars().all()
    assert len(actions) == 1, "exactly the admin attempt is audited"


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
