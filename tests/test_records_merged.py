"""Records, review and deliveries on one page; multi-select that holds (items 2, 3).

Two owner requests, one page:

* item 2 — merge the records/review page with the deliveries page, and stop
  saying "سیاست تلاش دوباره", use "Retry Policy";
* item 3 — select several theses at once.

The part of item 3 worth testing is NOT the checkbox (it existed) but that the
selection SURVIVES the table's 15-second auto-refresh. A plain checkbox is
wiped every time htmx swaps the table, so the operator ticks four rows, waits,
and approves nothing — silently. The selection now lives in a JS Set keyed by
record id and is re-applied after each swap.
"""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    Channel,
    Delivery,
    Main,
    PanelUser,
    Role,
)
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession, username: str = "merge-admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _login(client, username: str = "merge-admin"):
    response = await client.post(
        "/login", data={"username": username, "password": "secret123"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert (await client.get("/records", follow_redirects=False)).status_code == 200


async def _records(session: AsyncSession, n: int = 3) -> list[Main]:
    rows = []
    for i in range(n):
        row = Main(
            username=f"u{i}", title=f"Thesis {i}", abstract="A", field="پزشکی",
            university="U", author="A", supervisor="S", status="review",
            output_json={"title": f"Thesis {i}", "lead": "L", "body": "B"},
        )
        session.add(row)
        await session.commit()
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# item 2: one page
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_tabs_live_on_the_records_page(session: AsyncSession):
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        records = await client.get("/records")
        deliveries = await client.get("/records?tab=deliveries")
    assert "رکوردها و بازبینی" in records.text
    assert "ارسال‌ها و تحویل" in deliveries.text
    assert "/records?tab=deliveries" in records.text
    assert 'href="/records"' in deliveries.text


@pytest.mark.asyncio
async def test_the_retry_card_uses_english(session: AsyncSession):
    """The owner asked for the Persian phrase to go, not the card."""
    await _admin(session)
    channel = Channel(platform="bale", chat_id=93001, is_active=True)
    session.add(channel)
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records?tab=deliveries")
    assert "Retry Policy" in page.text
    assert "سیاست تلاش دوباره" not in page.text


@pytest.mark.asyncio
async def test_the_old_deliveries_url_redirects(session: AsyncSession):
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        response = await client.get("/deliveries", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/records?tab=deliveries"
        landed = await client.get(response.headers["location"])
    assert landed.status_code == 200


def test_the_standalone_deliveries_page_is_gone():
    from pathlib import Path

    assert not Path("app/panel/templates/deliveries.html").exists()


@pytest.mark.asyncio
async def test_deliveries_still_list_and_filter(session: AsyncSession):
    """Merging must not cost the deliveries tab its data or its filters."""
    await _admin(session)
    first = Channel(platform="bale", chat_id=93011, is_active=True)
    second = Channel(platform="bale", chat_id=93012, is_active=True)
    session.add_all([first, second])
    await session.commit()
    article = (await _records(session, 1))[0]
    # One delivery per (article, channel), so use both channels.
    session.add(Delivery(main_id=article.id, channel_id=first.id, status="sent",
                         platform_message_id=777))
    session.add(Delivery(main_id=article.id, channel_id=second.id, status="failed",
                         error_message="boom"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        everything = await client.get("/records?tab=deliveries")
        only_failed = await client.get("/records?tab=deliveries&status=failed")
    # The message id is shown only for a row in the result set, so it is what
    # the filter actually controls. (The "boom" text also appears via
    # last_error in the footer, which is not a row.)
    assert "777" in everything.text
    assert "777" not in only_failed.text, "the status filter actually filters"
    assert only_failed.status_code == 200


@pytest.mark.asyncio
async def test_the_htmx_partial_still_works(session: AsyncSession):
    await _admin(session)
    channel = Channel(platform="bale", chat_id=93021, is_active=True)
    session.add(channel)
    await session.commit()
    article = (await _records(session, 1))[0]
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="pending"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        partial = await client.get("/deliveries/table")
    assert partial.status_code == 200
    assert "<table" in partial.text


@pytest.mark.asyncio
async def test_no_link_points_at_the_removed_page(session: AsyncSession):
    from pathlib import Path

    for name in Path("app/panel/templates").glob("*.html"):
        html = name.read_text(encoding="utf-8")
        assert 'href="/deliveries"' not in html, f"{name} still links to the merged page"


# --------------------------------------------------------------------------
# item 3: multi-select
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_several_theses_can_be_selected_and_approved(session: AsyncSession):
    """The whole point: one action approves several theses."""
    await _admin(session)
    rows = await _records(session, 3)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records")
        for row in rows:
            assert f'value="{row.id}"' in page.text, "each reviewable row is selectable"
        response = await client.post(
            "/records/bulk-approve",
            data={"ids": [str(r.id) for r in rows]},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)
    for row in rows:
        await session.refresh(row)
        assert row.status == "approved"
    actions = (await session.execute(
        select(AuditLog.action).where(AuditLog.action == "approve")
    )).scalars().all()
    assert len(actions) == 3, "each approval is audited individually"


@pytest.mark.asyncio
async def test_select_all_control_is_present(session: AsyncSession):
    await _admin(session)
    await _records(session, 3)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records")
    assert "data-select-all" in page.text
    assert "انتخاب‌شده" in page.text, "a live count of the selection is shown"


@pytest.mark.asyncio
async def test_selection_survives_the_automatic_table_refresh(session: AsyncSession):
    """The silent failure this fixes: a 15s refresh wiped the selection.

    A plain checkbox lives in the DOM that htmx replaces, so ticking rows and
    waiting used to approve nothing — with no error to notice. The selection is
    now keyed by record id in a JS Set and re-applied after every swap.
    """
    await _admin(session)
    await _records(session, 2)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records")
    script = re.search(r"<script>(.*?)</script>", page.text, re.S)
    assert script, "the page carries the selection script"
    body = script.group(1)
    assert "selected = new Set()" in body, "ids are held outside the swapped DOM"
    assert "htmx:afterSwap" in body, "and re-applied after each swap"
    assert "selected.add" in body and "selected.delete" in body
    # The hidden inputs are rebuilt from the Set, so what is POSTed is exactly
    # what the counter claims is selected.
    assert 'hidden.name = \'ids\'' in body or 'hidden.name = "ids"' in body
    # And the submit is refused when nothing is selected, so no empty bulk
    # approve can fire by accident.
    assert "selected.size === 0" in body


@pytest.mark.asyncio
async def test_bulk_approve_still_ignores_non_reviewable_rows(session: AsyncSession):
    await _admin(session)
    rows = await _records(session, 2)
    rows[1].status = "published"
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            "/records/bulk-approve",
            data={"ids": [str(rows[0].id), str(rows[1].id)]},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)
    await session.refresh(rows[0])
    await session.refresh(rows[1])
    assert rows[0].status == "approved"
    assert rows[1].status == "published", "an already published article is untouched"
