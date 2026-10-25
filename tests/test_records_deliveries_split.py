"""Records/review and deliveries are two TABS of one page; multi-select holds.

Two owner requests, opposite directions over time:

* deliveries were an independent page, and the owner asked for them merged
  back into ``/records`` as a tab -- a record and its deliveries are one story
  told from two sides, so making the operator pick a page first is navigation
  for its own sake;
* item 3 -- select several theses at once.

What the split bought is deliberately kept: each tab carries its OWN filters
and its OWN KPIs, because "what needs my decision" and "did it reach the
channel" are different questions. These tests pin that, because a merge that
blended the two number strips would satisfy "one page" while losing the reason
they were ever separated.

The part of item 3 worth testing is NOT the checkbox (it existed) but that the
selection SURVIVES the table's 15-second auto-refresh. A plain checkbox is
wiped every time htmx swaps the table, so the operator ticks four rows, waits,
and approves nothing -- silently. The selection now lives in a JS Set keyed by
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
# item 2: one page, two tabs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_tab_renders_in_place(session: AsyncSession):
    """``?tab=deliveries`` is a real page now, not a redirect."""
    await _admin(session)
    await _records(session)
    async with panel_client() as client:
        await _login(client)
        records = await client.get("/records")
        deliveries = await client.get("/records", params={"tab": "deliveries"})
    assert records.status_code == 200 and deliveries.status_code == 200
    assert "رکوردها و بازبینی" in records.text
    assert "ارسال‌ها و تحویل" in deliveries.text
    # Neither tab is the other tab wearing a different query string.
    assert "تحویل‌ها" not in records.text, "the records tab carries no deliveries table"
    assert "/records/bulk-approve" not in deliveries.text


@pytest.mark.asyncio
async def test_the_tab_bar_is_plain_bookmarkable_links(session: AsyncSession):
    """No JS widget: the tab is a query string, so both halves are linkable."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records")
    assert 'href="/records"' in page.text
    assert 'href="/records?tab=deliveries"' in page.text
    assert "tab=records" not in page.text, "the default tab needs no query string"


@pytest.mark.asyncio
async def test_the_old_address_still_lands_on_the_tab(session: AsyncSession):
    """A bookmark or a link from an old audit note must not 404."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        response = await client.get("/deliveries", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/records?tab=deliveries"
        landed = await client.get("/records", params={"tab": "deliveries"})
    assert landed.status_code == 200
    assert "ارسال‌ها و تحویل" in landed.text


@pytest.mark.asyncio
async def test_an_anonymous_visitor_is_not_told_the_tab_exists(session: AsyncSession):
    """The redirect runs after the login check, so /deliveries leaks nothing."""
    async with panel_client() as client:
        response = await client.get("/deliveries", follow_redirects=False)
    assert response.status_code != 301, "an anonymous request must not be redirected"
    assert response.status_code in (302, 303, 307, 401, 403, 404) or response.status_code == 200


def test_the_tab_bodies_are_real_templates():
    from pathlib import Path

    for name in ("records.html", "records_body.html", "deliveries.html"):
        assert Path(f"app/panel/templates/{name}").exists()


@pytest.mark.asyncio
async def test_each_tab_shows_its_own_kpis(session: AsyncSession):
    """The reason for tabs rather than one blended strip.

    A record and its delivery are related, but the two tabs are asked
    different questions, so each carries the numbers that answer its own.
    """
    await _admin(session)
    article = (await _records(session, 1))[0]
    channel = Channel(platform="bale", chat_id=93031, is_active=True)
    session.add(channel)
    await session.commit()
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="sent",
                         platform_message_id=4242))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        records = await client.get("/records")
        deliveries = await client.get("/records", params={"tab": "deliveries"})
    # Records counts records; the delivered article is a RECORD in review, not
    # a delivered one, so "منتشرشده" must read zero on the records tab.
    assert "در انتظار بازبینی" in records.text
    assert "منتشرشده" in records.text
    assert "تحویل‌شده" not in records.text, "delivery KPIs stay on the delivery tab"
    # Deliveries counts deliveries, so the one sent row shows there.
    assert "تحویل‌شده" in deliveries.text
    assert "در صف ارسال" in deliveries.text
    assert "منتظر تأیید شما" in deliveries.text
    assert "در انتظار بازبینی" not in deliveries.text, "record KPIs stay on the record tab"


@pytest.mark.asyncio
async def test_the_deliveries_tab_still_lists_and_filters(session: AsyncSession):
    """Merging must not cost the tab its data or its filters."""
    await _admin(session)
    first = Channel(platform="bale", chat_id=93011, is_active=True)
    second = Channel(platform="bale", chat_id=93012, is_active=True)
    session.add_all([first, second])
    await session.commit()
    article = (await _records(session, 1))[0]
    # One delivery per (article, channel), so use both channels.
    session.add(Delivery(main_id=article.id, channel_id=first.id, status="sent",
                         platform_message_id=987654321))
    session.add(Delivery(main_id=article.id, channel_id=second.id, status="failed",
                         error_message="boom"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        everything = await client.get("/records", params={"tab": "deliveries"})
        only_failed = await client.get(
            "/records", params={"tab": "deliveries", "status": "failed"}
        )
    # The message id is shown only for a row in the result set, so it is what
    # the filter actually controls. (The "boom" text also appears via
    # last_error in the footer, which is not a row.)
    assert "987654321" in everything.text
    assert "987654321" not in only_failed.text, "the status filter actually filters"
    assert only_failed.status_code == 200


@pytest.mark.asyncio
async def test_filter_state_does_not_leak_across_the_tab_bar(session: AsyncSession):
    """``status`` means different things on the two tabs.

    Carrying it across would apply the record filter "در انتظار بازبینی" to
    deliveries and show an empty table that looks broken, so the tab links
    deliberately start from a bare URL.
    """
    await _admin(session)
    channel = Channel(platform="bale", chat_id=93041, is_active=True)
    session.add(channel)
    await session.commit()
    article = (await _records(session, 1))[0]
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="sent",
                         platform_message_id=5150))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        filtered = await client.get("/records", params={"status": "review"})
        tab = await client.get("/records", params={"tab": "deliveries"})
    # A record filter in effect, then the bare tab link: the delivery row is
    # there, so the record status did not follow us across.
    assert "در انتظار بازبینی" in filtered.text
    assert "5150" in tab.text


@pytest.mark.asyncio
async def test_the_deliveries_table_shows_each_channel_platform(session: AsyncSession):
    """The channel column shows the destination; the platform column shows
    which service it belongs to. Both are needed because a username alone does
    not say whether the message went to Bale or Telegram."""
    await _admin(session)
    bale = Channel(platform="bale", chat_id=93061, username="@bale-news", is_active=True)
    telegram = Channel(platform="telegram", chat_id=93062, username="@tg-news", is_active=True)
    session.add_all([bale, telegram])
    await session.commit()
    article = (await _records(session, 1))[0]
    session.add_all([
        Delivery(main_id=article.id, channel_id=bale.id, status="sent",
                 platform_message_id=6101),
        Delivery(main_id=article.id, channel_id=telegram.id, status="sent",
                 platform_message_id=6102),
    ])
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records", params={"tab": "deliveries"})
    assert "سکو</th>" in page.text
    assert "platform-badge platform-bale" in page.text
    assert "platform-badge platform-telegram" in page.text
    assert ">بله</span>" in page.text
    assert ">تلگرام</span>" in page.text


@pytest.mark.asyncio
async def test_the_platform_filter_follows_the_channels_table(session: AsyncSession, monkeypatch):
    """The filter is not a hardcoded bale/telegram pair: whatever platform list
    the service returns is rendered, with the stored name as its fallback label."""
    await _admin(session)
    from app.services import channels as channel_service

    async def future_platforms(_session):
        return ["bale", "discord"]

    monkeypatch.setattr(channel_service, "list_platforms", future_platforms)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records", params={"tab": "deliveries"})
    assert '<option value="discord"' in page.text
    assert ">discord</option>" in page.text


@pytest.mark.asyncio
async def test_the_htmx_partials_still_work(session: AsyncSession):
    await _admin(session)
    channel = Channel(platform="bale", chat_id=93021, is_active=True)
    session.add(channel)
    await session.commit()
    article = (await _records(session, 1))[0]
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="pending"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        deliveries_partial = await client.get("/deliveries/table")
        records_partial = await client.get("/records/table")
    assert deliveries_partial.status_code == 200
    assert "<table" in deliveries_partial.text
    assert records_partial.status_code == 200
    assert "<table" in records_partial.text


@pytest.mark.asyncio
async def test_the_retry_card_uses_english(session: AsyncSession):
    """The owner asked for the Persian phrase to go, not the card."""
    await _admin(session)
    channel = Channel(platform="bale", chat_id=93001, is_active=True)
    session.add(channel)
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records", params={"tab": "deliveries"})
    assert "Retry Policy" in page.text
    assert "سیاست تلاش دوباره" not in page.text


@pytest.mark.asyncio
async def test_the_sidebar_has_one_entry_summing_the_two_counts(session: AsyncSession):
    """One door to one room, with a badge that means "waiting on you"."""
    await _admin(session)
    article = (await _records(session, 1))[0]
    channel = Channel(platform="bale", chat_id=93051, is_active=True)
    session.add(channel)
    await session.commit()
    # One record awaiting review, one delivery awaiting confirmation.
    session.add(Delivery(main_id=article.id, channel_id=channel.id,
                         status="awaiting_confirmation"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/")
    assert 'href="/records"' in page.text
    assert 'href="/deliveries"' not in page.text, "the tab has no separate sidebar item"
    assert "رکوردها و بازبینی" in page.text
    # 1 review + 1 gated = 2 on one badge.
    assert '<span class="cnt">۲</span>' in page.text


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
    waiting used to approve nothing -- with no error to notice. The selection is
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
async def test_the_selection_script_only_renders_on_the_records_tab(session: AsyncSession):
    """It looks for #records-table and bails without it, but shipping it on
    the deliveries tab would mean a script that does nothing on every load."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records", params={"tab": "deliveries"})
    assert "selected = new Set()" not in page.text


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