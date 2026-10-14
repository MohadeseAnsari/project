"""The channels page, designed (frontend).

The redesign is driven by what the page is actually FOR, which the old layout
hid: news goes to every active channel, so the only two things an operator
configures are which category a field belongs to, and which channel stands for
a category.

Two things this file pins, because both were real defects rather than taste:

* the field → category table rendered EVERY field. A deployment has hundreds
  (542 here), so the page was unusable and the control you need is as rare as
  the one you want to change — it is now searched and paginated;
* the field editor appeared on two pages, so it has to say which page to
  return to.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Channel, Field, PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client

PAGE_SIZE = 50


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="ui-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _login(client):
    response = await client.post(
        "/login", data={"username": "ui-admin", "password": "secret123"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert (await client.get("/channels", follow_redirects=False)).status_code == 200


async def _world(session: AsyncSession, fields: int = 3) -> None:
    medical = Category(name="پزشکی و علوم سلامت", description="Medical")
    engineering = Category(name="فنی و مهندسی", description="Engineering")
    session.add_all([medical, engineering])
    for index in range(2):
        channel = Channel(platform="bale", chat_id=94001 + index,
                          username=f"@c{index}", title=f"کانال {index}", is_active=index == 0)
        session.add(channel)
    await session.commit()
    for i in range(fields):
        session.add(Field(name=f"رشته {i}", normalized_name=f"field-{i}",
                          category_id=medical.id if i % 2 == 0 else engineering.id,
                          matched_keyword=f"کلید {i}", category_source="rules", source="seed"))
    await session.commit()


# --------------------------------------------------------------------------
# the rule is stated up front
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_rule_is_stated_once_and_clearly(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert "همهٔ کانال‌های فعال" in page.text
    assert "ارسال را محدود نمی‌کند" in page.text
    assert "خاموش کنید" in page.text, "it says how to actually exclude a channel"


@pytest.mark.asyncio
async def test_a_summary_of_the_page_is_visible(session: AsyncSession):
    """Channels, active channels, categories, fields — before scrolling."""
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    for label in ("کانال", "کانال فعال", "دسته", "رشته"):
        assert label in page.text, f"the summary should mention {label}"


# --------------------------------------------------------------------------
# the field table is usable at scale
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_field_table_is_paginated_not_the_whole_database(session: AsyncSession):
    """542 fields rendered as rows is why the old page was unusable."""
    await _admin(session)
    await _world(session, fields=PAGE_SIZE + 20)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    rendered = page.text.count('action="/fields/')
    assert rendered <= PAGE_SIZE, f"only one page may render, got {rendered}"
    assert "از" in page.text and "رشته" in page.text, "a count is shown"


@pytest.mark.asyncio
async def test_the_field_table_is_searchable(session: AsyncSession):
    await _admin(session)
    await _world(session, fields=PAGE_SIZE + 20)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels", params={"fq": "رشته 3"})
    assert "رشته 3" in page.text
    # Unrelated rows are not dragged in.
    assert 'action="/fields/' in page.text
    assert "جست‌وجوی رشته" in page.text


@pytest.mark.asyncio
async def test_an_empty_search_result_is_explained(session: AsyncSession):
    await _admin(session)
    await _world(session, fields=2)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels", params={"fq": "no-such-field-xyz"})
    assert page.status_code == 200
    assert "پیدا نشد" in page.text


@pytest.mark.asyncio
async def test_paging_is_an_htmx_partial_that_keeps_its_place(session: AsyncSession):
    """Only the table swaps, so the channel settings are not thrown away."""
    await _admin(session)
    await _world(session, fields=PAGE_SIZE + 5)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
        assert 'hx-target="#fields-table"' in page.text
        partial = await client.get("/channels/table", params={"page": 2})
    assert partial.status_code == 200
    assert "<table" in partial.text
    assert "قبلی" in partial.text, "page 2 can go back"


# --------------------------------------------------------------------------
# the two assignments are clearly separate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_assignments_are_distinct_and_labelled(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert "رشته ← دسته" in page.text
    assert "دسته ← کانال متناظر" in page.text
    # The first is editable per field, the second per category.
    assert "/channels/category-channel" in page.text
    assert "/fields/" in page.text and "/category" in page.text


@pytest.mark.asyncio
async def test_editing_a_field_returns_to_the_page_it_came_from(session: AsyncSession):
    """Both pages offer this control, so the redirect has to be honest."""
    await _admin(session)
    await _world(session)
    field = (await session.execute(select(Field))).scalars().first()
    engineering = (await session.execute(
        select(Category).where(Category.name == "فنی و مهندسی")
    )).scalar_one()
    async with panel_client() as client:
        await _login(client)
        from_channels = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": str(engineering.id), "return_to": "channels"},
            follow_redirects=False,
        )
        from_fields = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": str(engineering.id), "return_to": "fields"},
            follow_redirects=False,
        )
    assert from_channels.status_code == 303
    assert from_channels.headers["location"] == "/channels"
    assert from_fields.headers["location"] == "/fields"


@pytest.mark.asyncio
async def test_return_to_is_whitelisted_not_echoed(session: AsyncSession):
    """An open redirect here would bounce an admin off-site from a form."""
    await _admin(session)
    await _world(session)
    field = (await session.execute(select(Field))).scalars().first()
    category = (await session.execute(select(Category))).scalars().first()
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": str(category.id), "return_to": "https://evil.example/pwn"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location in ("/fields", "/channels"), f"unexpected redirect: {location}"
    assert "evil.example" not in location


# --------------------------------------------------------------------------
# the channel list and its controls
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_channel_row_has_its_own_controls(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    for channel_id in (1, 2):
        assert f"/channels/{channel_id}/toggle" in page.text
        assert f"/channels?channel_id={channel_id}" in page.text
    assert "کانال فعال" in page.text, "active vs inactive is summarised"


@pytest.mark.asyncio
async def test_the_delete_action_is_visually_separated_from_save(session: AsyncSession):
    """Muscle memory should not reach a destructive action from Save."""
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert "danger-zone" in page.text
    assert page.text.index("ذخیرهٔ تغییرات") < page.text.index("danger-zone")


@pytest.mark.asyncio
async def test_add_channel_is_collapsed_until_asked(session: AsyncSession):
    """The add form is long; it should not dominate the page by default."""
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        collapsed = await client.get("/channels")
        expanded = await client.get("/channels", params={"new": "1"})
    assert "افزودن کانال تازه" in collapsed.text
    # The channel SETTINGS form also has a bot_token field, so assert on the
    # add card's own submit button rather than on a field name.
    assert "بررسی و افزودن کانال" not in collapsed.text, "the add form is not open by default"
    assert "بررسی و افزودن کانال" in expanded.text


@pytest.mark.asyncio
async def test_empty_state_offers_the_add_form(session: AsyncSession):
    """With no channels there must be a way to add the first one."""
    await _admin(session)
    session.add(Category(name="پزشکی و علوم سلامت", description="Medical"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert page.status_code == 200
    assert 'name="target"' in page.text
    assert "هنوز کانالی ثبت نشده" in page.text


def test_the_new_css_classes_exist():
    from pathlib import Path

    css = Path("app/panel/static/panel.css").read_text(encoding="utf-8")
    for selector in (".chan-row", ".inline-edit", ".danger-zone"):
        assert f"{selector}{{" in css, f"{selector} must be defined in panel.css"
