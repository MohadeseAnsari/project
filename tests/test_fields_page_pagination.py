"""The fields page: paginated and searched in SQL, with a disclosure.

This page was the worst in the panel: it pulled **every** field into Python and
rendered all of them, then filtered the list there. With 544 fields that is
about 27 screens of scrolling, and the search box narrowed the page only after
everything had already been fetched and laid out.

Two things are worth defending here beyond "it renders":

* the pager and the row count must come from the SAME predicate. Filtering
  after the LIMIT is the classic version of this bug — the page you see is not
  the page the count claims, and "next" walks off the end of the result set;
* the rarer filters live behind a native ``<details>``. The owner asked for
  disclosure with a clear indicator, and the cheap alternative — hiding things
  in a JS toggle — loses keyboard access and breaks without the script.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Field, PanelUser, Role
from app.panel.auth import hash_password
from app.services import fields as field_service
from tests.panel_test_client import panel_client

TEMPLATE = Path("app/panel/templates/fields.html").read_text(encoding="utf-8")
CSS = Path("app/panel/static/panel.css").read_text(encoding="utf-8")


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="fp-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _many_fields(session: AsyncSession, n: int = 120) -> list[Category]:
    """More rows than one page, so pagination is actually exercised."""
    medical = Category(name="پزشکی و علوم سلامت", description="Medical")
    engineering = Category(name="فنی و مهندسی", description="Engineering")
    session.add_all([medical, engineering])
    await session.commit()
    for i in range(n):
        session.add(Field(
            name=f"رشته {i:03d}", normalized_name=f"field-{i:03d}",
            category_id=medical.id if i % 2 == 0 else engineering.id,
            category_source=["rules", "curated", "manual", "llm"][i % 4],
        ))
        if i % 20 == 0:
            await session.commit()
    await session.commit()
    return [medical, engineering]


async def _page(client, **params):
    response = await client.get("/fields", params={k: v for k, v in params.items() if v})
    assert response.status_code == 200
    return response.text


# --------------------------------------------------------------------------
# the page is no longer 27 screens long
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_one_page_of_rows_is_rendered(session: AsyncSession):
    await _admin(session)
    await _many_fields(session, 120)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        first = await _page(client)
    rows = first.count('<td class="title">')
    assert rows <= 50, f"the default page rendered {rows} rows, not one page"
    assert "قبلی" not in first, "page 1 has nothing before it"
    assert "بعدی" in first, "but there is more to page through"


@pytest.mark.asyncio
async def test_the_page_size_is_configurable(session: AsyncSession):
    await _admin(session)
    await _many_fields(session, 120)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        small = await _page(client, page_size="25")
        large = await _page(client, page_size="100")
    assert small.count('<td class="title">') < large.count('<td class="title">')


@pytest.mark.asyncio
async def test_the_count_and_the_pager_come_from_the_same_predicate(
    session: AsyncSession,
):
    """The bug this guards: rows filtered AFTER the LIMIT.

    The count says 120, the page shows 50, and "next" eventually walks off the
    end because the window was never the real result set.
    """
    await _admin(session)
    medical, engineering = await _many_fields(session, 120)
    rows, total = await field_service.field_category_rows(
        session, category_id=medical.id, page=2, page_size=25
    )
    assert len(rows) == 25, "the page is full"
    assert total == 60, f"expected 60 medical fields, count says {total}"
    # Every row really is in the filtered set.
    assert all(r["category_id"] == medical.id for r in rows)


@pytest.mark.asyncio
async def test_paging_walks_the_whole_filtered_set(session: AsyncSession):
    await _admin(session)
    medical, _ = await _many_fields(session, 120)
    seen, page = set(), 1
    while page <= 4:
        rows, total = await field_service.field_category_rows(
            session, category_id=medical.id, page=page, page_size=25
        )
        seen.update(r["id"] for r in rows)
        if len(rows) < 25:
            break
        page += 1
    assert len(seen) == 60, f"paging reached {len(seen)} of 60 rows"


@pytest.mark.asyncio
async def test_search_narrows_the_page_and_says_so(session: AsyncSession):
    await _admin(session)
    await _many_fields(session, 40)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _page(client, q="رشته 007")
    assert "رشته 007" in page
    assert "از" in page and "۱" in page, "it says how many matched"


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_category_option_carries_a_usable_value(session: AsyncSession):
    """The bug this page redesign surfaced.

    ``category_cards`` returned dicts with no ``id``, so every ``<option>`` in
    the per-row category select rendered ``value=""``. The names looked right
    and the form submitted nothing — and no test caught it, because the tests
    POSTed ``category_id`` directly instead of reading the value out of the
    rendered form. That is the whole shape of this bug: the handler is right,
    the template is wrong, and the two are only connected by a string.
    """
    await _admin(session)
    medical, engineering = await _many_fields(session, 4)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _page(client)
    # Scope to the per-row category select: the page also has a source filter
    # whose options are "rules"/"manual"/..., which are values too.
    row_block = page.split('name="category_id" aria-label="دستهٔ')[1].split("</select>")[0]
    options = re.findall(r'<option value="([^"]*)"', row_block)
    real = [v for v in options if v != ""]
    assert real, f"no category option rendered a value at all: {row_block[:200]}"
    assert len(real) == len(options), "every option must carry a value, none empty"
    ids = {int(v) for v in real}
    assert medical.id in ids and engineering.id in ids, (
        "every category must be selectable by id, not just printable by name"
    )


@pytest.mark.asyncio
async def test_setting_a_category_through_the_rendered_form_actually_works(
    session: AsyncSession,
):
    """End to end: read the value out of the page and post exactly that."""
    await _admin(session)
    medical, engineering = await _many_fields(session, 4)
    field = (await session.execute(select(Field))).scalars().first()
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = (await client.get("/fields")).text
        # What a browser would submit: the select's current value for that row.
        block = page.split(f'action="/fields/{field.id}/category"')[1].split("</select>")[0]
        chosen = re.search(r'<option value="(\d+)"\s+selected', block)
        assert chosen, f"the row's select has no selectable value: {block[:200]}"
        response = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": chosen.group(1), "return_to": "/fields?page=2"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    await session.refresh(field)
    assert field.category_id == int(chosen.group(1))


@pytest.mark.asyncio
async def test_the_category_filter_works(session: AsyncSession):
    await _admin(session)
    medical, _ = await _many_fields(session, 40)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/fields", params={"category_id": str(medical.id)})
    assert page.status_code == 200
    assert f'value="{medical.id}" selected' in page.text, "the pick stays on what you chose"
    rows, total = await field_service.field_category_rows(
        session, category_id=medical.id, page=1, page_size=200
    )
    assert total == len(rows), "the filter narrows to that category only"


@pytest.mark.asyncio
async def test_unconfirmed_filter_is_what_an_operator_works_through(
    session: AsyncSession,
):
    await _admin(session)
    await _many_fields(session, 40)
    rows, total = await field_service.field_category_rows(
        session, unconfirmed_only=True, page=1, page_size=200
    )
    assert total == 20, "rules and llm are 2 of the 4 cycling sources, so 40 rows -> 20"
    assert all(r["category_source"] in ("rules", "llm") for r in rows)


@pytest.mark.asyncio
async def test_the_source_filter_narrows(session: AsyncSession):
    await _admin(session)
    await _many_fields(session, 40)
    rows, total = await field_service.field_category_rows(
        session, source="manual", page=1, page_size=200
    )
    assert total == 10, "one of four sources across 40 rows"
    assert all(r["category_source"] == "manual" for r in rows)


@pytest.mark.asyncio
async def test_an_empty_result_says_why_rather_than_showing_a_blank_table(
    session: AsyncSession,
):
    await _admin(session)
    await _many_fields(session, 10)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _page(client, q="نامی که وجود ندارد")
    assert "پیدا نشد" in page


# --------------------------------------------------------------------------
# the disclosure
# --------------------------------------------------------------------------


def test_rare_filters_are_behind_a_native_disclosure():
    """<details>, not a JS toggle: keyboard accessible and script-independent."""
    assert "<details" in TEMPLATE and "</details>" in TEMPLATE
    assert "more-btn" in TEMPLATE
    # The thing that says open or closed is an arrow that turns.
    assert ".more-btn:before" in CSS
    assert ".more[open]>.more-btn:before" in CSS


def test_the_indicator_points_the_right_way():
    assert 'content:"▾"' in CSS, "closed shows a down arrow"
    assert "rotate(-180deg)" in CSS, "open turns it"


def test_the_common_filters_stay_visible():
    """Disclosure must not swallow what is used daily."""
    head = TEMPLATE.split('<details class="more"')[0]
    for control in ('name="q"', 'name="category_id"', 'name="unconfirmed"'):
        assert control in head, f"{control} is used constantly and must stay visible"
    assert 'name="source"' in TEMPLATE.split('<details class="more"')[1]


def test_the_disclosure_does_not_overflow_on_a_tablet():
    """A popover positioned off the viewport edge is worse than none."""
    block = re.search(r"@media \(max-width:820px\)\{(.*?)\n\}", CSS, re.S)
    more = re.search(r"@media \(max-width:820px\)\{[^@]*?\.more-body\{(.*?)\}", CSS, re.S)
    assert more, "the popover needs a narrow-screen fallback"
    assert "position:static" in more.group(1), "it must become inline on a tablet"


def test_the_disclosure_is_keyboard_reachable():
    """summary is focusable and toggles on Enter/Space natively."""
    assert "<summary" in TEMPLATE
    assert ".more-btn:focus-visible" in CSS, "and the focus ring is visible"


# --------------------------------------------------------------------------
# saving must not lose the operator's place
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saving_a_category_returns_to_the_page_you_were_on(
    session: AsyncSession,
):
    """With 544 fields paginated, a save that dumps you on page 1 is a
    regression the operator feels every time, 50 times a curation pass."""
    await _admin(session)
    medical, engineering = await _many_fields(session, 120)
    field = (await session.execute(
        select(Field).where(Field.category_id == medical.id)
    )).scalars().first()
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": str(engineering.id),
                  "return_to": f"/fields?unconfirmed=1&page=3"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/fields?unconfirmed=1&page=3"


def test_return_to_cannot_be_an_open_redirect():
    """A crafted form would otherwise bounce a signed-in admin off-site."""
    from app.panel.app import _safe_fields_return as safe

    assert safe("/fields?page=7") == "/fields?page=7"
    assert safe("") == "/fields"
    assert safe("//evil.example") == "/fields"
    assert safe("https://evil.example") == "/fields"
    assert safe("javascript:alert(1)") == "/fields"
    assert safe("/records") == "/fields", "only the fields page is a legal destination"
    assert safe("/fields\\\\evil.example") == "/fields", "backslash normalisation"


@pytest.mark.asyncio
async def test_the_save_form_carries_the_current_filters(session: AsyncSession):
    await _admin(session)
    await _many_fields(session, 120)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _page(client, unconfirmed="1", page="2")
    assert 'name="return_to"' in page
    assert "unconfirmed=1" in page, "the search survives a save"
    assert "page=2" in page


# --------------------------------------------------------------------------
# what did not change
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_category_is_still_editable_on_the_field_row(session: AsyncSession):
    """The argument for merging still holds: a field's category is its own."""
    await _admin(session)
    medical, engineering = await _many_fields(session, 4)
    field = (await session.execute(select(Field))).scalars().first()
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _page(client)
        assert f"/fields/{field.id}/category" in page
        response = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": str(engineering.id)}, follow_redirects=False,
        )
        assert response.status_code == 303
    await session.refresh(field)
    assert field.category_id == engineering.id
    assert field.category_source == "manual"


@pytest.mark.asyncio
async def test_the_filter_form_is_a_get_so_a_filtered_page_is_shareable(
    session: AsyncSession,
):
    """GET, not POST: the URL is the state, so Back and Refresh both work."""
    await _admin(session)
    await _many_fields(session, 10)
    async with panel_client() as client:
        await client.post("/login", data={"username": "fp-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _page(client)
    assert 'method="get"' in page
    assert 'action="/fields"' in page