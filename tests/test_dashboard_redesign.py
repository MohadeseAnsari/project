"""The dashboard answers "what needs me", and everything it claims is real.

Two things are pinned here, both of which were wrong:

* **the header pill used to be a literal string.** It rendered "API ok · DB
  connected" unconditionally — directly above a card that computes the actual
  model and worker state. A disconnected model still showed a green "ok". A
  status indicator that cannot report bad news is worse than none, because it
  is trusted precisely when it matters.

* **the KPI cards were not clickable.** Four numbers describing four queues,
  none of which could be acted on without re-finding the list by hand and
  re-deriving the filter. They are now links to exactly the list each counts.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, Delivery, Main, PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client

DASH = Path("app/panel/templates/dashboard.html").read_text(encoding="utf-8")
LIVE = Path("app/panel/templates/dashboard_live.html").read_text(encoding="utf-8")


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="dash-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _world(session: AsyncSession) -> Channel:
    channel = Channel(platform="bale", chat_id=96001, username="@d", title="D", is_active=True)
    article = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                   author="A", supervisor="S", status="published",
                   output_json={"title": "T", "lead": "L", "body": "B"})
    session.add_all([channel, article])
    await session.commit()
    session.add(Delivery(main_id=article.id, channel_id=channel.id, status="sent",
                         platform_message_id=1))
    await session.commit()
    return channel


# --------------------------------------------------------------------------
# the counters must be actionable
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_kpi_is_a_link_to_the_list_it_counts(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/")
    cards = re.findall(r'<a class="kpi[^"]*" href="([^"]+)"', page.text)
    assert len(cards) == 4, f"expected four linked cards, found {len(cards)}"
    joined = " ".join(cards)
    assert "/records?status=review" in joined, "the review queue must be reachable"
    assert "/records?status=ready" in joined, "the generation queue must be reachable"
    assert "/deliveries?status=sent" in joined
    assert "/deliveries?status=failed" in joined


@pytest.mark.asyncio
async def test_every_kpi_link_actually_resolves(session: AsyncSession):
    """A dashboard full of dead links is worse than a dashboard without them."""
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/")
        for href in re.findall(r'<a class="kpi[^"]*" href="([^"]+)"', page.text):
            response = await client.get(href)
            assert response.status_code == 200, f"{href} -> {response.status_code}"


def test_the_kpi_cards_are_marked_up_as_links():
    assert "kpi-link" in LIVE, "the row needs the link styling hook"
    assert "kpi-alert" in LIVE, "a non-zero failure count must stand out"


# --------------------------------------------------------------------------
# the status indicator must be able to report bad news
# --------------------------------------------------------------------------


def test_the_header_pill_is_not_a_hardcoded_string():
    """The bug: the old pill rendered unconditionally.

    Comments are stripped first: the fix explains what it removed, so a naive
    substring check would fail on its own documentation.
    """
    rendered = re.sub(r"{#.*?#}", "", DASH, flags=re.S)
    rendered = re.sub(r"\{#.*?#\}", "", rendered, flags=re.S)
    assert "API ok" not in rendered, "the pill must not claim health it never checked"
    assert "DB connected" not in rendered
    assert "model_state" in DASH, "it must read the computed state"
    assert "worker_fresh" in DASH


@pytest.mark.asyncio
async def test_the_pill_follows_the_model_state(session: AsyncSession, monkeypatch):
    """Both directions, or it is decoration.

    The pill reads BOTH the model state and the worker heartbeat, so both are
    pinned here — otherwise a test that only varies the model would still see
    "not healthy" from an idle worker and would pass for the wrong reason.
    """
    from app.services import dashboard as dashboard_service

    await _admin(session)
    await _world(session)

    async def counters(state: str, worker: bool):
        async def fake(session):
            return {
                "kpi_review": 0, "kpi_queued": 0, "kpi_sent": 0, "kpi_failed": 0,
                "worker_fresh": worker, "pending_deliveries": 0,
                "model_health": {"name": "m", "api_model": "gpt", "state": state,
                                 "tested_at": None, "latency_ms": None, "error": "boom"},
            }
        return fake

    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        # Only the model changes: everything else is held identical.
        monkeypatch.setattr(dashboard_service, "live_counters", await counters("connected", True))
        good = (await client.get("/")).text
        monkeypatch.setattr(dashboard_service, "live_counters", await counters("disconnected", True))
        broken_model = (await client.get("/")).text
        monkeypatch.setattr(dashboard_service, "live_counters", await counters("connected", False))
        idle_worker = (await client.get("/")).text

    assert "همه‌چیز سالم" in good
    assert "همه‌چیز سالم" not in broken_model, "a disconnected model must not show healthy"
    assert "نیازمند بررسی" in broken_model
    assert "همه‌چیز سالم" not in idle_worker, "an idle worker must not show healthy either"


# --------------------------------------------------------------------------
# empty states, and the chart's own numbers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_dashboard_says_so_instead_of_showing_blank_cards(
    session: AsyncSession,
):
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        page = (await client.get("/")).text
    assert "هنوز رکوردی در هفت روز گذشته ثبت نشده" in page, "the chart has an empty state"
    assert "هنوز کانالی ثبت نشده" in page, "the channel card has one"
    # The timeline is NOT empty here: logging in writes an audit row, which is
    # the point of an audit trail. Its empty state is asserted on the template.
    assert "{% if timeline %}" in DASH and "هنوز رویدادی ثبت نشده" in DASH


@pytest.mark.asyncio
async def test_the_chart_reports_records_not_days(session: AsyncSession):
    """A count of days-with-data labelled "records" would be a quiet lie."""
    await _admin(session)
    for i in range(3):
        session.add(Main(username=f"u{i}", title=f"T{i}", abstract="A", field="پزشکی",
                         university="U", author="A", supervisor="S", status="published",
                         output_json={"title": "T", "lead": "L", "body": "B"}))
        await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        page = (await client.get("/")).text
    total = re.search(r"مجموع ([۰-۹0-9]+) رکورد", page)
    assert total, "the chart must state its own total"
    digits = total.group(1).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    assert int(digits) >= 3, f"said {digits} records, but at least 3 exist"


def test_the_health_rows_share_one_component():
    """Five copies of the same inline flex is how they drift apart."""
    assert "class=\"hrow\"" in LIVE
    assert LIVE.count("class=\"hrow\"") == 5
    assert ".health{" in Path("app/panel/static/panel.css").read_text(encoding="utf-8")
    # The old version repeated this inline style on every row.
    assert LIVE.count("display:flex;align-items:center;gap:8px") <= 1


def test_the_failed_note_links_to_the_queue_it_describes():
    """It says "go and retry"; it should carry the link."""
    assert "/deliveries?status=failed" in DASH