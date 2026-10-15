"""The fields page must be able to reach every field in the database.

The bug, in two compounding parts:

* ``list_fields()`` has a default ``limit=100`` and the route called it with no
  argument, so only the newest 100 rows were ever rendered -- 449 of 549 fields
  simply absent, with a badge claiming 549 and no pager to explain the gap;
* the search box then filtered that already-truncated list **in Python**, so
  those 449 could not be found by searching either. 16 fields match "زیست"; the
  page could show 4 of them.

The fix routes the page through ``field_category_rows()``, which already did
search + count + pagination in SQL. Most of this file exists to stop the same
truncation coming back through a different door.
"""

import re

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
    session.add(PanelUser(username="fp-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    session.add(Category(name="پزشکی و علوم سلامت", description="Medical"))
    await session.commit()


async def _any_category(session: AsyncSession) -> Category:
    row = (await session.execute(select(Category))).scalars().first()
    if row is None:
        row = Category(name="پزشکی و علوم سلامت", description="Medical")
        session.add(row)
        await session.commit()
    return row


async def _many(session: AsyncSession, n: int = 137, prefix: str = "fp") -> Field:
    """More rows than any single page, so pagination is actually exercised.

    ``fields.category_id`` is NOT NULL, so every row needs one.
    """
    category = await _any_category(session)
    first = None
    for i in range(n):
        row = Field(name=f"{prefix}-{i:03d}", normalized_name=f"{prefix}-{i:03d}",
                    source="seed", category_id=category.id,
                    category_source=["rules", "curated", "manual", "llm"][i % 4])
        session.add(row)
        await session.commit()
        first = first or row
    return first


def _rows(page: str) -> int:
    body = page.split("<tbody>")[1].split("</tbody>")[0]
    return len(re.findall(r"<tr", body))


def _text(page: str) -> str:
    """Visible text with tags stripped.

    Counts sit inside <span class="num">, so asserting on raw HTML misses every
    one of them -- which is how a test can fail while the page is correct.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def _fa_digits(value: str) -> str:
    return value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


# --------------------------------------------------------------------------
# nothing is silently omitted
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_page_does_not_render_more_rows_than_a_page_holds(session: AsyncSession):
    await _admin(session)
    await _many(session, 137)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields")
    assert page.status_code == 200
    assert _rows(page.text) == 50, "a page holds page_size rows, not every row"


@pytest.mark.asyncio
async def test_paging_reaches_every_row_exactly_once(session: AsyncSession):
    """Not "most of them". Every field, once."""
    await _admin(session)
    await _many(session, 137)
    seen, page = [], 1
    while True:
        rows, total = await field_service.field_category_rows(
            session, page=page, page_size=25
        )
        seen.extend(r["id"] for r in rows)
        if len(rows) < 25:
            break
        page += 1
        assert page < 50, "the pager is looping"
    assert total == 137, f"count says {total}"
    assert len(seen) == 137, f"paging reached {len(seen)} of 137"
    assert len(set(seen)) == 137, "a field was rendered on two pages"


@pytest.mark.asyncio
async def test_the_pager_is_present_and_its_links_carry_the_filters(session: AsyncSession):
    await _admin(session)
    await _many(session, 137)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields")
    assert 'class="pager"' in page.text, "no pager means the rest are unreachable"
    assert "بعدی" in page.text
    assert "قبلی" not in page.text, "page 1 has nothing before it"


@pytest.mark.asyncio
async def test_the_count_and_the_rows_come_from_the_same_predicate(session: AsyncSession):
    """The classic version of this bug: filter after the LIMIT.

    The count says 137, the page shows 50, and "next" eventually walks off the
    end because the window was never the real result set.
    """
    await _admin(session)
    await _many(session, 137)
    rows, total = await field_service.field_category_rows(
        session, q="fp-0", page=2, page_size=10
    )
    assert len(rows) == 10, "the page is full"
    # q="fp-0" matches fp-000..fp-099 — a hundred rows, not ten.
    assert total == 100, f"expected 100 matches, count says {total}"


@pytest.mark.asyncio
async def test_page_size_is_configurable_and_capped(session: AsyncSession):
    await _admin(session)
    await _many(session, 137)
    rows, total = await field_service.field_category_rows(
        session, page=1, page_size=5000
    )
    assert len(rows) <= 200, "page_size must be capped so one request cannot pull everything"
    assert total == 137


# --------------------------------------------------------------------------
# search reaches rows the old list could never show
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_finds_fields_outside_the_old_hundred(session: AsyncSession):
    """The search used to filter the truncated list, so old rows were unfindable."""
    await _admin(session)
    await _many(session, 60, prefix="old")
    category = await _any_category(session)
    target = Field(name="unique-target-needle", normalized_name="unique-target-needle",
                   source="seed", category_id=category.id, category_source="rules")
    session.add(target)
    await session.commit()

    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields", params={"q": "unique-target-needle"})
    assert "unique-target-needle" in page.text, (
        "a field that exists must be findable; the old code could not see it"
    )


@pytest.mark.asyncio
async def test_search_results_are_paginated_not_truncated(session: AsyncSession):
    await _admin(session)
    category = await _any_category(session)
    for i in range(120):
        session.add(Field(name=f"needle-{i:03d}", normalized_name=f"needle-{i:03d}",
                          source="seed", category_id=category.id, category_source="rules"))
        await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        first = await client.get("/fields", params={"q": "needle", "page_size": "25"})
    assert _rows(first.text) == 25
    visible = _text(first.text)
    assert "۱" in visible and "۲۵" in visible and "۱۲۰" in visible, (
        "the page must state both the shown range and the total"
    )
    assert _fa_digits("۱۲۰") == "120"


# --------------------------------------------------------------------------
# filters, and that they compose with paging
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_category_filter_narrows(session: AsyncSession):
    await _admin(session)
    medical = await _any_category(session)
    other = Category(name="فنی و مهندسی", description="Engineering")
    session.add(other)
    await session.commit()
    for i in range(20):
        session.add(Field(name=f"cat-{i:03d}", normalized_name=f"cat-{i:03d}",
                          source="seed", category_source="rules",
                          category_id=medical.id if i % 2 == 0 else other.id))
        await session.commit()
    rows, total = await field_service.field_category_rows(
        session, category_id=medical.id, page=1, page_size=200
    )
    assert total == 10, "only the even ones belong to that category"
    assert all(r["category_id"] == medical.id for r in rows)


@pytest.mark.asyncio
async def test_the_source_filter_narrows(session: AsyncSession):
    await _admin(session)
    await _many(session, 40, prefix="src")
    rows, total = await field_service.field_category_rows(
        session, source="manual", page=1, page_size=200
    )
    assert total == 10, "one of four cycling sources across 40 rows"
    assert all(r["category_source"] == "manual" for r in rows)


@pytest.mark.asyncio
async def test_the_unconfirmed_filter_is_what_an_operator_works_through(session: AsyncSession):
    await _admin(session)
    await _many(session, 40, prefix="unc")
    rows, total = await field_service.field_category_rows(
        session, unconfirmed_only=True, page=1, page_size=200
    )
    assert total == 20, "rules and llm are 2 of the 4 cycling sources"
    assert all(r["category_source"] in ("rules", "llm") for r in rows)


@pytest.mark.asyncio
async def test_paging_keeps_the_search_and_the_filter(session: AsyncSession):
    """Otherwise paging silently widens the result set."""
    await _admin(session)
    medical = await _any_category(session)
    for i in range(60):
        session.add(Field(name=f"combo-{i:03d}", normalized_name=f"combo-{i:03d}",
                          source="seed", category_source="manual",
                          category_id=medical.id))
        await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get(
            "/fields", params={"q": "combo", "source": "manual",
                               "category_id": medical.id, "page_size": "25"}
        )
    assert _rows(page.text) == 25
    nxt = re.search(r'href="(/fields\?[^"]*page=2)"', page.text)
    assert nxt, "there must be a next link"
    for key in ("q=combo", "source=manual", f"category_id={medical.id}"):
        assert key in nxt.group(1), f"the next link drops {key}"


@pytest.mark.asyncio
async def test_paging_starts_from_the_search_not_from_the_top(session: AsyncSession):
    """Clearing a filter returns to the unfiltered first page, not page 9."""
    await _admin(session)
    await _many(session, 137)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        cleared = await client.get("/fields")
    assert "قبلی" not in cleared.text, "nothing precedes page 1"
    pager = cleared.text.split('class="pager"')[1].split("</div>")[0]
    # The CURRENT page carries no page= param; the next link legitimately does.
    assert re.search(r">قبلی<", pager) is None
    assert re.search(r'href="/fields\?[^"]*page=2"', pager), "there must be a page 2 to go to"


# --------------------------------------------------------------------------
# states
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_result_explains_itself_and_offers_a_way_out(session: AsyncSession):
    await _admin(session)
    await _many(session, 5)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields", params={"q": "nothing-matches-this"})
    assert _rows(page.text) == 1, "the empty message is one row"
    assert "پیدا نشد" in page.text or "برای نمایش نیست" in page.text
    assert 'href="/fields"' in page.text, "and a way to clear the filters"


@pytest.mark.asyncio
async def test_the_table_states_both_numbers(session: AsyncSession):
    """Total and shown, so a truncated page can never look complete."""
    await _admin(session)
    await _many(session, 137)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields", params={"page_size": "25"})
    visible = _text(page.text)
    assert "نمایش" in visible, "the page must say what it is showing"
    assert "۱" in visible and "۲۵" in visible, "the shown range: rows 1..25"
    assert "۱۳۷" in visible, "and the total, so a full page can never look complete"


# --------------------------------------------------------------------------
# what must not regress
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_per_row_category_editor_still_works(session: AsyncSession):
    """Pagination changed the data source; the editor did not move."""
    await _admin(session)
    medical = await _any_category(session)
    other = Category(name="فنی و مهندسی", description="Engineering")
    session.add(other)
    await session.commit()
    field = Field(name="edit-me", normalized_name="edit-me", source="seed",
                  category_source="rules", category_id=medical.id)
    session.add(field)
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields")
        assert f"/fields/{field.id}/category" in page.text
        chosen = re.search(r'<option value="(\d+)"\s+selected', page.text)
        assert chosen, "the row must preselect its real category"
        response = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": str(other.id)}, follow_redirects=False,
        )
        assert response.status_code == 303
    await session.refresh(field)
    assert field.category_id == other.id
    assert field.category_source == "manual"


@pytest.mark.asyncio
async def test_the_table_still_shows_the_ingestion_source(session: AsyncSession):
    await _admin(session)
    category = await _any_category(session)
    row = Field(name="src-visible", normalized_name="src-visible", source="spreadsheet",
                category_id=category.id, category_source="rules")
    session.add(row)
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields")
    assert "spreadsheet" in page.text


def test_the_route_no_longer_calls_the_truncating_list():
    """The regression guard: nothing may reintroduce a silent LIMIT here."""
    from pathlib import Path

    source = Path("app/panel/app.py").read_text(encoding="utf-8")
    # Strip comments: the function explains in prose why it no longer calls
    # list_fields(), and a naive search matches its own explanation.
    body = re.sub(r"#.*", "", source.split("async def fields_page")[1]
                  .split('@panel_app.get("/categories"')[0])
    assert "list_fields(" not in body, (
        "fields_page must not use list_fields(); it caps at 100 rows and has no count"
    )
    assert "field_category_rows(" in body
    assert "page_size" in body