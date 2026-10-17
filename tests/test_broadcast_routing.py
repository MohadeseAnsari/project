"""The category->channel matrix is LIVE, and it is the only thing that routes.

This file used to be called ``test_broadcast`` and pinned the opposite rule:
every active channel receives the news. That rule is gone. What replaced it is
narrower and easier to get wrong, so these tests pin it from both sides:

* the matrix DOES filter (a category reaches only its selected channels);
* ``main.field_channel_routes`` stays inert -- the per-field exception table
  still exists in the schema but must not resurrect as a second routing path,
  because then two tables would each be able to decide where news goes and
  they would disagree.

``fields.category_id`` is not routing: it is the CLASSIFICATION the generator
and the records use, and it lives on the fields page. That is what decides
which row of the matrix a record reads.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
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
from tests.routing_setup import route_category


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
# the rule: the matrix filters
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_channels_the_matrix_names_are_targets(session: AsyncSession):
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    await route_category(session, category, [channels[0]])
    assert [c.id for c in await channels_for_article(session, article)] == [channels[0].id]


@pytest.mark.asyncio
async def test_an_empty_matrix_sends_nothing(session: AsyncSession):
    """Not "everything", which is what a fallback would do."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    assert await channels_for_article(session, article) == []


@pytest.mark.asyncio
async def test_switching_a_channel_off_removes_it_from_every_category(session: AsyncSession):
    """A channel is a destination, not a rule: off means off everywhere."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    await route_category(session, category, channels)
    channels[1].is_active = False
    await session.commit()
    assert [c.id for c in await channels_for_article(session, article)] == [
        channels[0].id, channels[2].id
    ]


@pytest.mark.asyncio
async def test_provenance_names_the_category_that_decided(session: AsyncSession):
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    await route_category(session, category, [channels[0]])
    pairs = await channels_for_article_with_source(session, article)
    assert {(c.id, source) for c, source in pairs} == {(channels[0].id, "category")}


# --------------------------------------------------------------------------
# the per-field table must stay inert
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_field_route_alone_routes_nothing(session: AsyncSession):
    """A field->channel row is not a destination.

    The schema still carries this table. If it ever became a second routing
    path, two tables could each decide where news goes and they would disagree
    the moment an operator set them differently -- which the panel gives no way
    to even see, since the matrix is the only surface it renders.
    """
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channels[0].id))
    await session.commit()
    assert await channels_for_article(session, article) == []


@pytest.mark.asyncio
async def test_a_field_route_does_not_add_to_the_matrix(session: AsyncSession):
    """It must not widen the matrix either."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    await route_category(session, category, [channels[0]])
    session.add(FieldChannelRoute(field_id=field.id, channel_id=channels[2].id))
    await session.commit()
    assert [c.id for c in await channels_for_article(session, article)] == [channels[0].id]


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
    """Classification kept its home; routing lives on /categories."""
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
# editing the matrix
# --------------------------------------------------------------------------


def test_the_channels_service_offers_a_replace_not_an_add():
    """``add_*_route`` helpers stay gone: the checkbox list is the whole truth."""
    for gone in ("set_category_channel", "add_field_route", "add_category_route"):
        assert not hasattr(channel_service, gone), f"{gone} is dead code now"
    assert hasattr(channel_service, "category_channel_matrix")
    assert hasattr(channel_service, "set_category_channels")


@pytest.mark.asyncio
async def test_replacing_a_category_matrix_removes_the_old_rows(session: AsyncSession):
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
async def test_a_saved_matrix_actually_changes_where_news_goes(session: AsyncSession):
    """The end-to-end claim: edit the picker, and delivery follows."""
    category, channels = await _world(session)
    field = (await session.execute(select(Field))).scalars().one()
    article = await _article(session, category, field)
    assert await channels_for_article(session, article) == []

    await channel_service.set_category_channels(session, category.id, [channels[2].id])
    assert [c.id for c in await channels_for_article(session, article)] == [channels[2].id]


# --------------------------------------------------------------------------
# the panel must not mislead about it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_categories_page_says_routing_applies_now(session: AsyncSession):
    await _admin(session)
    category, _channels = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/categories")
    assert page.status_code == 200
    assert f"/categories/{category.id}/channels" in page.text
    assert "فعلاً اعمال نمی‌شود" not in page.text, (
        "the page must not still claim the mapping is recorded but not applied"
    )
    assert "هیچ خبری منتشر نمی‌کند" in page.text, (
        "and must say plainly that an unrouted category publishes nothing"
    )


@pytest.mark.asyncio
async def test_the_page_warns_how_much_work_has_no_destination(session: AsyncSession):
    """Otherwise "no news arrived" is the only symptom."""
    await _admin(session)
    category, _channels = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        before = await client.get("/categories")
        await channel_service.set_category_channels(session, category.id, [1])
        after = await client.get("/categories")
    assert "در دسته‌هایی هستند که هیچ کانالی ندارند" in before.text
    assert "بدون مسیر" in before.text
    # Once the route exists the warning is gone, not merely reworded.
    assert "در دسته‌هایی هستند که هیچ کانالی ندارند" not in after.text


@pytest.mark.asyncio
async def test_the_warning_respects_the_search_filter(session: AsyncSession):
    """Naming a filtered-out category would defeat the search it sits above."""
    await _admin(session)
    await _world(session)
    other = Category(name="فنی و مهندسی", description="Engineering")
    session.add(other)
    await session.commit()
    session.add(Field(name="another-field", normalized_name="another-field",
                      category_id=other.id))
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/categories", params={"q": "فنی"})
    assert "فنی و مهندسی" in page.text
    assert "پزشکی و علوم سلامت" not in page.text


@pytest.mark.asyncio
async def test_the_channels_page_shows_which_categories_name_it(session: AsyncSession):
    """Read-only mirror, so editing a channel shows what points at it."""
    await _admin(session)
    category, channels = await _world(session)
    await channel_service.set_category_channels(session, category.id, [channels[0].id])
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/channels", params={"channel_id": channels[0].id})
    assert "دسته‌های مسیریابی‌شده به این کانال" in page.text
    assert "پزشکی و علوم سلامت" in page.text
    assert "/categories/" not in page.text, "the mirror is read-only; no second editor"


@pytest.mark.asyncio
async def test_saving_the_matrix_is_admin_only_and_audited(session: AsyncSession):
    category, channels = await _world(session)
    await _admin(session, username="matrix-admin")
    viewer_role = (await session.execute(
        select(Role).where(Role.name == "viewer"))).scalar_one_or_none()
    if viewer_role is None:
        viewer_role = Role(name="viewer", description="viewer")
        session.add(viewer_role)
        await session.flush()
    session.add(PanelUser(username="matrix-viewer", password_hash=hash_password("secret123"),
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
        select(AuditLog.action).where(AuditLog.action == "category_channels")
    )).scalars().all()
    assert len(actions) == 1, "exactly the admin attempt is audited"


from app.db.models import AuditLog  # noqa: E402  (used only by the audit test)
