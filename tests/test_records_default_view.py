"""The records page must show the records it exists to show.

``approved`` used to default to ``"no"`` -- "not yet approved". That was
defensible while the panel was mostly a review queue. It stopped being true the
moment a record reached ``published``, which is the entire purpose of the
panel: a published record is not "not yet approved", so it stopped being listed,
and the page rendered blank.

Worse, the «منتشرشده» status tab could **never** match anything, because the
default filter excluded exactly the status that tab selects. A permanently
broken control that looked perfectly normal.

The dashboard's review card links straight at ``?status=review&approved=no``, so
the useful default is preserved without being imposed on every visitor.
"""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Main, PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="rec-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _records(session: AsyncSession, status: str, n: int = 2) -> list[Main]:
    rows = []
    for i in range(n):
        row = Main(
            username=f"u{i}", title=f"Thesis {status} {i}", abstract="A", field="پزشکی",
            university="U", author="A", supervisor="S", status=status,
            output_json={"title": "T", "lead": "L", "body": "B"},
        )
        session.add(row)
        await session.commit()
        rows.append(row)
    return rows


def _table(page: str) -> str:
    """Only the table body.

    Asserting titles against the whole page is a trap: the filter dropdowns
    render every category and university, so a value can be present without any
    row being filtered to it.
    """
    return page.split("<tbody>")[1].split("</tbody>")[0]


@pytest.mark.asyncio
async def test_published_records_are_visible_by_default(session: AsyncSession):
    """The bug, as the owner saw it: an empty page."""
    await _admin(session)
    await _records(session, "published")
    async with panel_client() as client:
        await client.post("/login", data={"username": "rec-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/records")
    assert page.status_code == 200
    assert "Thesis published" in _table(page.text), (
        "a published record must be listed by default; the page looked broken"
    )


@pytest.mark.asyncio
async def test_every_status_tab_can_actually_match(session: AsyncSession):
    """«منتشرشده» could never show anything while the default excluded it."""
    await _admin(session)
    await _records(session, "published")
    async with panel_client() as client:
        await client.post("/login", data={"username": "rec-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/records", params={"status": "published"})
    assert "Thesis published" in _table(page.text)


@pytest.mark.asyncio
async def test_the_pending_filter_is_still_one_click_away(session: AsyncSession):
    """The useful default is preserved, just not imposed."""
    await _admin(session)
    await _records(session, "published", n=1)
    await _records(session, "review", n=2)
    async with panel_client() as client:
        await client.post("/login", data={"username": "rec-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/records", params={"approved": "no"})
    body = _table(page.text)
    assert "Thesis review" in body
    assert "Thesis published" not in body, "the filter still narrows"


@pytest.mark.asyncio
async def test_the_page_and_its_htmx_partial_default_the_same(session: AsyncSession):
    """A mismatch loses the filter on the first 15s refresh, which looks random."""
    from app.panel.app import panel_app

    routes = {getattr(r, "path", None): r for r in panel_app.routes}
    page = routes["/records"]
    partial = routes["/records/table"]
    page_default = page.dependant.query_params[-1].default
    partial_default = partial.dependant.query_params[-1].default
    assert page_default == partial_default == "", (
        f"page defaults to {page_default!r}, partial to {partial_default!r}"
    )


@pytest.mark.asyncio
async def test_the_htmx_partial_agrees_with_the_page(session: AsyncSession):
    await _admin(session)
    await _records(session, "published")
    async with panel_client() as client:
        await client.post("/login", data={"username": "rec-admin", "password": "secret123"},
                          follow_redirects=False)
        partial = await client.get("/records/table")
    assert partial.status_code == 200
    assert "Thesis published" in _table(partial.text), (
        "otherwise the table empties itself on the first auto-refresh"
    )


@pytest.mark.asyncio
async def test_the_pending_filter_is_reachable_from_the_ui(session: AsyncSession):
    """Not by URL only — the control must be offered on the page."""
    await _admin(session)
    await _records(session, "review")
    async with panel_client() as client:
        await client.post("/login", data={"username": "rec-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/records")
    assert 'name="approved"' in page.text, "the approval filter must be offered"
    assert "نیازمند بازبینی" in page.text, "with a readable label for the pending queue"


def test_the_select_offers_all_first():
    """Order follows the default, so the control reads honestly."""
    template = __import__("pathlib").Path("app/panel/templates/records.html").read_text(
        encoding="utf-8")
    block = template.split('<select name="approved">')[1].split("</select>")[0]
    options = re.findall(r'value="([^"]*)"', block)
    assert options[0] == "", "«همه» must be the first option and the default"
    assert set(options) == {"", "no", "yes"}