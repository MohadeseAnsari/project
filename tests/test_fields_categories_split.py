"""Fields and categories are two independent pages.

They were merged into one page with two tabs, and the owner later asked for
them back as separate pages. The reasoning that justified the merge is still
worth keeping, though, and this file keeps testing it: a field IS a row, its
category IS a column on that row, and the matched keyword is why the column
holds what it holds. So the field table still carries category and keyword
inline with an editable select — editing a field's category stays on the field
row.

What changed is only the browsing job. ``/categories`` is a real page again,
listing the buckets and what falls into each. ``/fields?tab=categories`` still
redirects there, so old links and bookmarks work.
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
async def test_each_is_its_own_page(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        fields = await client.get("/fields")
        categories = await client.get("/categories")
    assert fields.status_code == 200 and categories.status_code == 200
    assert "رشته‌ها" in fields.text
    assert "دسته‌ها و کلیدواژه‌ها" in categories.text
    # The fields page LINKS to the categories page; it does not embed the list.
    # Category names do appear there, but only as <option>s of the per-row
    # editor, so the check is for the list's own chrome rather than its names.
    assert 'href="/categories"' in fields.text
    assert "تغییر دستهٔ یک رشته" not in fields.text
    assert "تغییر دستهٔ یک رشته" in categories.text


@pytest.mark.asyncio
async def test_the_sidebar_offers_both_pages_separately(session: AsyncSession):
    """The owner's actual requirement: independently reachable, not tabbed."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/")
    assert 'href="/fields"' in page.text
    assert 'href="/categories"' in page.text
    assert page.text.count('href="/categories"') == 1


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
async def test_the_categories_page_lists_categories_with_their_fields(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/categories")
    assert page.status_code == 200
    assert "پزشکی و علوم سلامت" in page.text
    assert "فنی و مهندسی" in page.text
    assert "قلب" in page.text, "a category shows the fields that belong to it"


@pytest.mark.asyncio
async def test_the_two_pages_cross_link(session: AsyncSession):
    """Each page points at the other, so neither is a dead end."""
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        fields_page = await client.get("/fields")
        categories_page = await client.get("/categories")
    assert 'href="/categories"' in fields_page.text
    assert 'href="/fields"' in categories_page.text


@pytest.mark.asyncio
async def test_the_categories_page_can_be_searched(session: AsyncSession):
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/categories", params={"q": "فنی"})
    assert "فنی و مهندسی" in page.text
    assert "پزشکی و علوم سلامت" not in page.text, "the filter actually filters"


@pytest.mark.asyncio
async def test_a_category_with_no_fields_says_so(session: AsyncSession):
    """An empty bucket is information; a blank card is a bug."""
    await _admin(session)
    session.add(Category(name="دستهٔ خالی", description="Empty"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/categories")
    assert "دستهٔ خالی" in page.text
    assert "هنوز رشته‌ای در این دسته نیست" in page.text


# --------------------------------------------------------------------------
# old address
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_old_tab_url_redirects_instead_of_404(session: AsyncSession):
    """A bookmark or a link from an old email must not 404."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        response = await client.get("/fields", params={"tab": "categories"},
                                    follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/categories"
        landed = await client.get("/categories")
    assert landed.status_code == 200
    assert "دسته" in landed.text


def test_the_standalone_page_is_a_real_template():
    from pathlib import Path

    assert Path("app/panel/templates/categories.html").exists()


def test_no_link_goes_through_the_old_tab_url():
    """The RECORDS/fields pages must not resurrect the removed ?tab=categories view.

    Exception: ``record_detail.html`` now has its own ``?tab=categories`` tab —
    "دسته‌بندی‌های خودکار" (v2-combined) — a per-record classification view, not
    the removed global fields/categories split.
    """
    from pathlib import Path

    for name in Path("app/panel/templates").glob("*.html"):
        if name.name == "record_detail.html":
            continue
        html = name.read_text(encoding="utf-8")
        assert "tab=categories" not in html, f"{name} still links via the old tab URL"


# --------------------------------------------------------------------------
# editing still works from the field row
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
async def test_search_filters_the_fields_page(session: AsyncSession):
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
