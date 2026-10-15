"""One page for fields, categories and keywords (item 4).

The owner asked to merge the three and fix their structure. They were split
because they were written at different times, not because the concepts are
different: a field IS a row, its category IS a column on that row, and the
matched keyword is why the column holds what it holds. Splitting them meant
changing one thing in one tab while the consequence sat in another.

So this is ONE page with two tabs, and the field table carries category and
keyword inline with an editable category select. ``/categories`` redirects to
the categories tab rather than 404ing, so old links and bookmarks work.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Field, PanelUser, Role
from app.panel.auth import hash_password
from app.services import fields as field_service
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="fc-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _seed(session: AsyncSession) -> None:
    medical = Category(name="پزشکی و علوم سلامت", description="Medical")
    engineering = Category(name="فنی و مهندسی", description="Engineering")
    session.add_all([medical, engineering])
    await session.commit()
    for name, category, keyword, source in (
        ("هوش مصنوعی", engineering, "هوش مصنوعی", "rules"),
        ("قلب", medical, "قلب", "curated"),
        ("عصب", medical, None, "rules"),
    ):
        session.add(Field(name=name, normalized_name=name, category_id=category.id,
                          matched_keyword=keyword, category_source=source, source="seed"))
    await session.commit()


async def _login(client):
    response = await client.post(
        "/login", data={"username": "fc-admin", "password": "secret123"}, follow_redirects=False
    )
    assert response.status_code == 302
    probe = await client.get("/fields", follow_redirects=False)
    assert probe.status_code == 200, "login did not establish a session"


# --------------------------------------------------------------------------
# one page, two tabs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_page_holds_fields_and_categories(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/fields")
    assert page.status_code == 200
    # Both concepts on one page, as tabs.
    assert "رشته‌ها" in page.text
    assert "دسته‌ها" in page.text
    # And the old separate page is gone from the sidebar.
    assert 'href="/categories"' not in page.text


@pytest.mark.asyncio
async def test_the_field_table_carries_category_and_keyword_inline(session: AsyncSession):
    """The structural fix: a field, its category and its keyword are one row."""
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/fields")
    assert "هوش مصنوعی" in page.text
    assert "فنی و مهندسی" in page.text, "the category is visible on the field's row"
    assert "کلیدواژه" in page.text
    assert "/category" in page.text, "the category is editable from the same row"


@pytest.mark.asyncio
async def test_categories_tab_lists_categories_with_their_fields(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/fields?tab=categories")
    assert page.status_code == 200
    assert "پزشکی و علوم سلامت" in page.text
    assert "فنی و مهندسی" in page.text
    assert "قلب" in page.text, "a category shows the fields that belong to it"


@pytest.mark.asyncio
async def test_the_two_tabs_are_both_reachable(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        fields_tab = await client.get("/fields")
        categories_tab = await client.get("/fields?tab=categories")
    assert "/fields?tab=categories" in fields_tab.text
    assert "/fields" in categories_tab.text


# --------------------------------------------------------------------------
# old address
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_old_categories_url_redirects_instead_of_404(session: AsyncSession):
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        response = await client.get("/categories", follow_redirects=False)
        assert response.status_code in (301, 302)
        landed = await client.get(response.headers["location"])
    assert landed.status_code == 200
    assert "دسته" in landed.text


def test_the_standalone_page_and_template_are_gone():
    from pathlib import Path

    assert not Path("app/panel/templates/categories.html").exists(), (
        "the separate categories page was merged into /fields"
    )


# --------------------------------------------------------------------------
# editing still works from the merged page
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_can_be_changed_from_the_field_row(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    field = (await session.execute(
        select(Field).where(Field.name == "هوش مصنوعی")
    )).scalar_one()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/fields")
        assert f"/fields/{field.id}/category" in page.text
        response = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": str((await session.execute(
                select(Category).where(Category.name == "پزشکی و علوم سلامت")
            )).scalar_one().id)},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)
    await session.refresh(field)
    assert field.category_source == "manual"


@pytest.mark.asyncio
async def test_search_filters_the_merged_page(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/fields?q=قلب")
    assert page.status_code == 200
    assert "قلب" in page.text
    assert "هوش مصنوعی" not in page.text, "the filter actually filters"


@pytest.mark.asyncio
async def test_field_rows_expose_everything_the_table_needs(session: AsyncSession):
    """The table renders from plain dicts, so the service supplies them all."""
    await _seed(session)
    rows, total = await field_service.field_category_rows(session)
    by_name = {r["name"]: r for r in rows}
    assert total == 3
    assert set(by_name) == {"هوش مصنوعی", "قلب", "عصب"}
    assert by_name["قلب"]["matched_keyword"] == "قلب"
    assert by_name["عصب"]["matched_keyword"] is None
    assert by_name["هوش مصنوعی"]["category_source"] == "rules"
    assert by_name["قلب"]["category_source"] == "curated"
