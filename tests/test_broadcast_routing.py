"""The category -> channel matrix is the only routing path.

A record reaches a channel only through its stored ``main_category_matches``:
the LLM writes those during generation, and a reviewer can replace them
manually. There is no field-name classification and no field-level routing.
"""

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    Category,
    Channel,
    Main,
    MainCategoryMatch,
    PanelUser,
    Role,
)
from app.messaging.dispatcher import channels_for_article_multi
from app.services import channels as channel_service
from app.services.passwords import hash_password
from tests.panel_test_client import panel_client
from tests.routing_setup import route_category

_channel_counter = itertools.count(93000)


async def _admin(session: AsyncSession, username: str = "route-admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _world(session: AsyncSession, category_name: str = "پزشکی و علوم سلامت") -> tuple[Category, list[Channel]]:
    category = Category(name=category_name, description=category_name)
    session.add(category)
    await session.commit()
    channels = []
    for index in range(3):
        chat_id = next(_channel_counter)
        channel = Channel(platform="bale", chat_id=chat_id, username=f"@c{chat_id}", is_active=True)
        session.add(channel)
        await session.commit()
        channels.append(channel)
    return category, channels


async def _article(session: AsyncSession, category: Category, *, rank: int = 1) -> Main:
    """Approved article WITH the qualifying match the matrix reads."""
    row = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
               author="A", supervisor="S", status="approved",
               category_id=category.id,
               output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(row)
    await session.flush()
    session.add(MainCategoryMatch(
        main_id=row.id, category_id=category.id, score=0.9, rank=rank, reason="test",
    ))
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_only_the_channels_the_matrix_names_are_targets(session: AsyncSession):
    category, channels = await _world(session)
    article = await _article(session, category)
    await route_category(session, category, [channels[0]])
    pairs = await channels_for_article_multi(session, article)
    assert [c.id for c, _source in pairs] == [channels[0].id]


@pytest.mark.asyncio
async def test_an_empty_matrix_sends_nothing(session: AsyncSession):
    """Not "everything", which is what a fallback would do."""
    category, _channels = await _world(session)
    article = await _article(session, category)
    assert await channels_for_article_multi(session, article) == []


@pytest.mark.asyncio
async def test_switching_a_channel_off_removes_it_from_every_category(session: AsyncSession):
    category, channels = await _world(session)
    article = await _article(session, category)
    await route_category(session, category, channels)
    channels[1].is_active = False
    await session.commit()
    pairs = await channels_for_article_multi(session, article)
    assert [c.id for c, _source in pairs] == [channels[0].id, channels[2].id]


@pytest.mark.asyncio
async def test_provenance_names_the_category_that_decided(session: AsyncSession):
    category, channels = await _world(session)
    article = await _article(session, category)
    await route_category(session, category, [channels[0]])
    pairs = await channels_for_article_multi(session, article)
    assert {(c.id, source) for c, source in pairs} == {(channels[0].id, category.name)}


@pytest.mark.asyncio
async def test_replacing_a_category_matrix_removes_the_old_rows(session: AsyncSession):
    category, channels = await _world(session)
    await channel_service.set_category_channels(session, category.id, [c.id for c in channels])
    await channel_service.set_category_channels(session, category.id, [channels[1].id])
    matrix = await channel_service.category_channel_matrix(session)
    assert [row["channel_ids"] for row in matrix if row["id"] == category.id] == [[channels[1].id]]


@pytest.mark.asyncio
async def test_an_empty_selection_clears_a_category(session: AsyncSession):
    category, channels = await _world(session)
    await channel_service.set_category_channels(session, category.id, [c.id for c in channels])
    assert await channel_service.set_category_channels(session, category.id, []) == []
    matrix = await channel_service.category_channel_matrix(session)
    assert [row["channel_ids"] for row in matrix if row["id"] == category.id] == [[]]


@pytest.mark.asyncio
async def test_a_saved_matrix_actually_changes_where_news_goes(session: AsyncSession):
    category, channels = await _world(session)
    article = await _article(session, category)
    assert await channels_for_article_multi(session, article) == []
    await channel_service.set_category_channels(session, category.id, [channels[2].id])
    pairs = await channels_for_article_multi(session, article)
    assert [c.id for c, _source in pairs] == [channels[2].id]


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
    assert "فعلاً اعمال نمی‌شود" not in page.text
    assert "هیچ خبری منتشر نمی‌کند" in page.text


@pytest.mark.asyncio
async def test_the_page_warns_how_much_work_has_no_destination(session: AsyncSession):
    """An unrouted category is counted in records, not fields."""
    await _admin(session)
    category, _channels = await _world(session)
    await _article(session, category)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        before = await client.get("/categories")
        await channel_service.set_category_channels(session, category.id, [1])
        after = await client.get("/categories")
    assert "در دسته‌هایی هستند که هیچ کانالی ندارند" in before.text
    assert "بدون مسیر" in before.text
    assert "در دسته‌هایی هستند که هیچ کانالی ندارند" not in after.text


@pytest.mark.asyncio
async def test_the_warning_respects_the_search_filter(session: AsyncSession):
    await _admin(session)
    await _world(session)
    other, _channels = await _world(session, "فنی و مهندسی")
    await _article(session, other)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/categories", params={"q": "فنی"})
    assert "فنی و مهندسی" in page.text
    assert "پزشکی و علوم سلامت" not in page.text


@pytest.mark.asyncio
async def test_the_channels_page_shows_which_categories_name_it(session: AsyncSession):
    await _admin(session)
    category, channels = await _world(session)
    await channel_service.set_category_channels(session, category.id, [channels[0].id])
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/channels", params={"channel_id": channels[0].id})
    assert "دسته‌های مسیریابی‌شده به این کانال" in page.text
    assert "پزشکی و علوم سلامت" in page.text
    assert "/categories/" not in page.text


@pytest.mark.asyncio
async def test_saving_the_matrix_is_admin_only_and_audited(session: AsyncSession):
    category, channels = await _world(session)
    await _admin(session, username="matrix-admin")
    viewer_role = (await session.execute(select(Role).where(Role.name == "viewer"))).scalar_one_or_none()
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
            data={"channel_ids": [str(c.id) for c in channels]}, follow_redirects=False,
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
    ]
    actions = (await session.execute(
        select(AuditLog.action).where(AuditLog.action == "category_channels")
    )).scalars().all()
    assert len(actions) == 1
