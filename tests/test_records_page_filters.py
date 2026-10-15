"""The records page: only the daily filters on the bar, the rest on request.

The owner's choice for this page was to collapse the rare filters and stop
lying about how many options exist. Two things are worth defending:

* **a filter that is applied but hidden is worse than no filter.** If a field
  filter is active and the disclosure is closed, the table quietly shows fewer
  rows than the operator expects, with nothing on screen saying why. So the
  button counts what it is hiding and the disclosure opens itself when any of
  those filters is active.

* **the status tabs must carry the other filters.** They are plain links. If
  they only set ``?status=``, clicking "منتشرشده" while a field filter was
  applied drops it without a word — the same lie as a pager that forgets the
  search box.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Main, PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client

TEMPLATE = Path("app/panel/templates/records.html").read_text(encoding="utf-8")


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="rp-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _records(session: AsyncSession) -> list[Main]:
    rows = []
    for i in range(6):
        row = Main(
            username=f"u{i}", title=f"Thesis {i}", abstract="A",
            field="پزشکی" if i % 2 == 0 else "فنی",
            university="تهران" if i % 2 == 0 else "شیراز",
            author="A", supervisor="S", status="review" if i < 3 else "published",
            output_json={"title": "T", "lead": "L", "body": "B"},
        )
        session.add(row)
        await session.commit()
        rows.append(row)
    return rows


def _table(page: str) -> str:
    """Just the table body.

    Asserting field names against the whole page is a trap: the filter
    disclosure renders EVERY field as an <option>, so "فنی" is on the page
    whether or not any row is filtered to it.
    """
    body = page.split("<tbody>")[1].split("</tbody>")[0]
    return body


async def _page(client, **params):
    response = await client.get("/records", params={k: v for k, v in params.items() if v})
    assert response.status_code == 200
    return response.text


async def _login(client):
    await client.post("/login", data={"username": "rp-admin", "password": "secret123"},
                      follow_redirects=False)


# --------------------------------------------------------------------------
# what is on the bar
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_daily_filters_stay_on_the_bar(session: AsyncSession):
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        page = await _page(client)
    bar = page.split('<details class="more"')[0]
    for control in ('name="q"', 'در انتظار بازبینی', 'منتشرشده'):
        assert control in bar, f"{control} is used daily and must stay visible"


@pytest.mark.asyncio
async def test_the_rare_filters_are_inside_the_disclosure(session: AsyncSession):
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        page = await _page(client)
    assert "<details" in page, "the rare filters need a disclosure"
    inside = page.split('<details class="more"')[1].split("</details>")[0]
    for control in ('name="field"', 'name="university"', 'name="approved"', 'name="order"'):
        assert control in inside, f"{control} belongs behind the disclosure"


def test_it_is_the_same_disclosure_component_as_the_fields_page():
    """One component, so the behaviour cannot drift between pages."""
    fields = Path("app/panel/templates/fields.html").read_text(encoding="utf-8")
    for token in ('<details class="more"', 'class="more-btn"', 'class="more-body"'):
        assert token in TEMPLATE and token in fields, f"{token} must be the shared component"


# --------------------------------------------------------------------------
# a hidden filter must still announce itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_button_counts_what_it_is_hiding(session: AsyncSession):
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        clean = await _page(client)
        filtered = await _page(client, field="پزشکی")
    assert "فیلترهای بیشتر" in clean
    assert "فیلترهای بیشتر (" not in clean, "no badge when nothing is hidden"
    assert "فیلترهای بیشتر (" in filtered, "an applied filter must be visible"
    assert "۱" in filtered.split("فیلترهای بیشتر")[1][:12], "and counted"


@pytest.mark.asyncio
async def test_an_applied_filter_opens_the_disclosure_by_itself(session: AsyncSession):
    """Otherwise the operator sees a filtered table with no visible cause."""
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        page = await _page(client, university="تهران")
    block = page.split('<details class="more"')[1].split(">")[0]
    assert "open" in block, "the disclosure must open when it holds an applied filter"


@pytest.mark.asyncio
async def test_the_filter_actually_still_applies(session: AsyncSession):
    """Collapsing a control must not disconnect it from the query."""
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        everything = await _page(client)
        medical = await _page(client, field="پزشکی")
    assert len(_table(everything).split("<tr>")) > len(_table(medical).split("<tr>")), (
        "the field filter must narrow the rows"
    )
    assert "فنی" not in _table(medical), "and no row from the other field survives"


# --------------------------------------------------------------------------
# status tabs must not silently drop a filter
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_tabs_keep_the_other_filters(session: AsyncSession):
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        page = await _page(client, field="پزشکی", q="Thesis")
    for href in re.findall(r'<a href="(/records\?[^"]*)"', page):
        assert "field=" in href, f"the status tab {href} drops the field filter"
        assert "q=" in href, f"the status tab {href} drops the search"


@pytest.mark.asyncio
async def test_a_tab_click_reaches_the_filtered_set(session: AsyncSession):
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        page = await _page(client, field="پزشکی")
        href = re.search(r'<a href="(/records\?[^"]*status=published[^"]*)"', page)
        assert href, "the published tab must exist"
        landed = await client.get(href.group(1))
    assert landed.status_code == 200
    assert "فنی" not in _table(landed.text), "the field filter survived the tab click"


@pytest.mark.asyncio
async def test_the_all_tab_has_no_status_but_keeps_the_rest(session: AsyncSession):
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        page = await _page(client, field="پزشکی")
    all_tab = re.search(r'<a href="(/records\?[^"]*)"[^>]*><span class="(?:on\s*)?">همه<', page)
    assert all_tab, "the همه tab must exist"
    assert "status=" not in all_tab.group(1)
    assert "field=" in all_tab.group(1)


# --------------------------------------------------------------------------
# the option list must not quietly truncate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_filter_options_cap_covers_the_reference_table(session: AsyncSession):
    """It was 50, and it silently stopped offering values as data grew."""
    from app.services.records import filter_options

    signature = filter_options.__defaults__ or ()
    assert signature, "filter_options must still have a default limit"
    assert signature[0] >= 500, (
        f"the cap is {signature[0]}; the field reference table alone has 544 rows"
    )


@pytest.mark.asyncio
async def test_the_search_box_already_covers_field_and_university(
    session: AsyncSession,
):
    """Why the pickers are the rare ones: this is the common path."""
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        by_field = await _page(client, q="فنی")
        by_uni = await _page(client, q="شیراز")
    assert "فنی" in _table(by_field), "search matches on the field column"
    # The fixture pairs field="فنی" with university="شیراز", so searching the
    # university finds the same rows: that is the point — one box, both axes.
    assert "فنی" in _table(by_uni), "search matches on the university column too"
    assert "پزشکی" not in _table(by_uni), "and it really filters"


# --------------------------------------------------------------------------
# what must not regress
# --------------------------------------------------------------------------


def test_the_multi_select_script_is_still_wired():
    """Selection survives the 15s refresh; the disclosure change must not
    have moved the ids that script depends on."""
    assert 'id="records-table"' in TEMPLATE
    assert 'id="bulk-form"' in TEMPLATE
    assert 'id="bulk-submit"' in TEMPLATE
    assert 'id="bulk-count"' in TEMPLATE
    assert "selected = new Set()" in TEMPLATE
    assert "htmx:afterSwap" in TEMPLATE


@pytest.mark.asyncio
async def test_bulk_approve_still_works_with_the_new_bar(session: AsyncSession):
    await _admin(session)
    rows = await _records(session)
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            "/records/bulk-approve",
            data={"ids": [str(r.id) for r in rows[:2]]}, follow_redirects=False,
        )
        assert response.status_code in (302, 303)
    for row in rows[:2]:
        await session.refresh(row)
        assert row.status == "approved"


@pytest.mark.asyncio
async def test_ordering_is_now_reachable_rather_than_a_hidden_input(session: AsyncSession):
    """``order`` was submitted as a hidden value nothing could ever change."""
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        oldest = await _page(client, order="oldest")
        newest = await _page(client)
    assert 'value="oldest" selected' in oldest, "the choice is reflected back"
    pattern = r'<td class="num">([۰-۹0-9]+)</td>'
    assert re.findall(pattern, oldest) == list(reversed(re.findall(pattern, newest))), (
        "ordering by oldest really reverses the list"
    )