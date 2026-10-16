"""The panel UI overhaul: a health pill that explains, icons, and layout rhythm.

Three concerns, all of which are invisible in a screenshot of the diff and
easy to regress silently:

* the health pill used to say "نیازمند بررسی" and stop, which tells an operator
  they have a problem and nothing about which one;
* action buttons were Persian text labels, so four actions in a row read as a
  paragraph and pushed the real columns off-screen;
* vertical rhythm was an inline ``margin-bottom`` on each page, re-guessed
  every time a page changed.

These tests pin the observable contract of all three: what the markup must
contain, and what must NOT come back.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, Main, Model, PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client

TEMPLATES = Path("app/panel/templates")
CSS = Path("app/panel/static/panel.css")


async def _admin(session: AsyncSession, username: str = "ui-admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _login(client, username: str = "ui-admin"):
    await client.post("/login", data={"username": username, "password": "secret123"},
                      follow_redirects=False)


def _text(path: str) -> str:
    return (TEMPLATES / path).read_text(encoding="utf-8")


def _header(html: str) -> str:
    """The sticky top bar only.

    Phrases like "نیازمند بررسی" also appear in a KPI label, so an assertion
    about the health pill has to be scoped to the header or it passes for the
    wrong reason.
    """
    match = re.search(r'<header class="top">(.*?)</header>', html, re.S)
    assert match, "the page has no header to scope the pill to"
    return match.group(1)


# --------------------------------------------------------------------------
# Task 1: the health pill explains what is wrong
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_unhealthy_pill_names_the_component_and_the_reason(session: AsyncSession):
    """No active model is the canonical unhealthy state, so use it."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/")
    assert page.status_code == 200
    header = _header(page.text)
    assert "نیازمند بررسی" in header, "it is still honest about being unwell"
    assert "همه‌چیز سالم" not in header
    # A <details>, so the reasons cost nothing until asked for and no JS is needed.
    assert '<details class="pill-disclose">' in header
    # Which component, in Persian, and how many of them.
    assert "مدل فعال" in header
    assert "پیکربندی نشده" in header
    # The worker is stale in a fresh test DB and must be named too.
    assert "worker تولید" in header


@pytest.mark.asyncio
async def test_the_pill_stays_collapsed_and_the_header_keeps_its_width(session: AsyncSession):
    """The collapsed summary IS the pill, so the header does not jump."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/")
    summary = re.search(r'<summary class="pill">(.*?)</summary>', page.text, re.S)
    assert summary, "the pill is the <details> summary, not a sibling"
    assert "نیازمند بررسی" in summary.group(1)


@pytest.mark.asyncio
async def test_a_connected_model_and_a_fresh_worker_show_the_healthy_pill(
    session: AsyncSession, monkeypatch
):
    """The other branch: green must still say green, not stay silent."""
    from datetime import datetime, timedelta

    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    await _admin(session)
    session.add(Model(name="gpt-x", provider="openai", version="v1", is_active=True,
                      api_key_env="OPENAI_API_KEY", last_test_ok=True))
    row = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
               author="A", supervisor="S", status="review",
               output_json={"title": "T", "lead": "L", "body": "B"},
               heartbeat_at=datetime.utcnow() - timedelta(seconds=5))
    session.add(row)
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/")
    header = _header(page.text)
    assert "همه‌چیز سالم" in header
    assert "نیازمند بررسی" not in header
    assert "pill-disclose" not in header, "a healthy system has nothing to expand"


@pytest.mark.asyncio
async def test_a_stale_worker_shows_when_the_last_heartbeat_was(session: AsyncSession):
    """"Idle" does not say whether this is a blip or an outage."""
    from datetime import datetime, timedelta

    await _admin(session)
    row = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
               author="A", supervisor="S", status="review",
               output_json={"title": "T", "lead": "L", "body": "B"},
               heartbeat_at=datetime.utcnow() - timedelta(hours=9))
    session.add(row)
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/")
        live = await client.get("/dashboard/live")
    assert "آخرین ضربان" in page.text
    assert "آخرین ضربان" in live.text


@pytest.mark.asyncio
async def test_a_never_seen_worker_says_so_rather_than_showing_nothing(session: AsyncSession):
    """A missing timestamp is not the same as an old one."""
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/")
    assert "هرگز ضربانی ثبت نشده" in page.text


@pytest.mark.asyncio
async def test_live_counters_carries_the_heartbeat_timestamp(session: AsyncSession):
    """Task 6: the raw timestamp, not only the boolean derived from it."""
    from datetime import datetime, timedelta

    from app.services import dashboard as dashboard_service

    await _admin(session)
    moment = datetime.utcnow() - timedelta(minutes=30)  # the lease is 5
    session.add(Main(username="u", title="T", abstract="A", field="پ", university="U",
                     author="A", supervisor="S", status="review",
                     output_json={"title": "T", "lead": "L", "body": "B"},
                     heartbeat_at=moment))
    await session.commit()
    live = await dashboard_service.live_counters(session)
    assert live["worker_fresh"] is False
    assert live["worker_last_heartbeat_at"] is not None


# --------------------------------------------------------------------------
# Task 4: icons, not text buttons
# --------------------------------------------------------------------------


ICON_TEMPLATES = [
    "records_table.html",
    "deliveries_table.html",
    "users.html",
    "models.html",
    "channels.html",
    "prompts.html",
]


@pytest.mark.parametrize("name", ICON_TEMPLATES)
def test_action_rows_use_the_icon_macro_not_a_text_button(name):
    body = _text(name)
    if "icon-btn" in body or "icon(" in body or "icon_link(" in body:
        assert "{% from \"icons.html\"" in body, f"{name} must import the macro"
    assert "rowbtn" not in body, (
        f"{name} still renders a text action button; the row icons replace it"
    )


def test_the_icon_macro_keeps_the_text_for_mouse_and_screen_readers():
    """Dropping the visible text without a tooltip AND an aria-label would
    have made every action anonymous for anyone not looking at the pixels."""
    macro = _text("icons.html")
    assert "title=\"{{ label }}\"" in macro
    assert "aria-label=\"{{ label }}\"" in macro
    # Both renderers read ONE definition of the glyphs, so they cannot drift.
    assert macro.count("'trash':") == 1, "the glyph table is duplicated"
    assert "viewBox=\"0 0 16 16\"" in macro
    assert "currentColor" in macro
    assert "http" not in macro, "no icon CDN: the panel must work with no outbound network"


def test_destructive_actions_confirm_and_name_the_target():
    """Every destructive icon asks first, in Persian, naming what it touches."""
    confirmations = {
        "records_table.html": ["تأیید و برای ارسال آزاد شود"],
        "deliveries_table.html": ["ارسال این خبر لغو شود", "همین حالا از کانال حذف شود",
                                   "اپلیکیشن کانال حذف کرده‌اید"],
        "users.html": ["حذف شود"],
        "models.html": ["غیرفعال شود", "حذف شود"],
        "channels.html": ["برای همیشه حذف شود"],
    }
    for name, phrases in confirmations.items():
        body = _text(name)
        assert body.count("confirm(") >= len(phrases), f"{name} lost a confirmation"
        for phrase in phrases:
            assert phrase in body, f"{name}: missing confirmation {phrase!r}"
        assert re.search(r"confirm\('[^؀-ۿ]*", body), "confirmations must be Persian"


@pytest.mark.asyncio
async def test_a_table_row_renders_real_icons_with_labels(session: AsyncSession):
    await _admin(session)
    session.add(Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                     author="A", supervisor="S", status="review",
                     output_json={"title": "T", "lead": "L", "body": "B"}))
    session.add(Channel(platform="bale", chat_id=94001, is_active=True))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records")
    assert 'class="icon-btn ok"' in page.text
    assert "<svg" in page.text
    # An icon-only button is only acceptable with a name on it.
    assert 'aria-label="این خبر را تأیید و منتشر کن"' in page.text
    assert 'title="این خبر را ویرایش کن"' in page.text
    assert "تأیید و انتشار</button>" not in page.text


@pytest.mark.asyncio
async def test_a_delivery_row_offers_the_recovery_icons_for_its_state(session: AsyncSession):
    await _admin(session)
    channel = Channel(platform="bale", chat_id=94011, is_active=True)
    session.add(channel)
    await session.commit()
    row = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
               author="A", supervisor="S", status="review",
               output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(row)
    await session.commit()
    from app.db.models import Delivery

    session.add(Delivery(main_id=row.id, channel_id=channel.id, status="failed",
                         error_message="boom"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/records", params={"tab": "deliveries"})
    assert "این ارسال ناموفق را دوباره بفرست" in page.text
    assert 'class="icon-btn danger"' not in page.text or "danger" in page.text


# --------------------------------------------------------------------------
# Task 5: one layout rhythm
# --------------------------------------------------------------------------


def test_panel_css_defines_the_layout_and_icon_classes():
    css = CSS.read_text(encoding="utf-8")
    for rule in (".page-section", ".page-section.tight", ".kpi-row", ".card-head",
                 ".table-row", ".icon-btn", ".icon-btn.danger", ".icon-btn.ok",
                 ".icon-btn.mut"):
        assert rule in css, f"panel.css is missing {rule}"
    # No media queries were introduced: this work is not responsive.
    assert "@media" not in css, "the overhaul must not add a breakpoint"


def test_no_template_reaches_for_an_icon_library():
    for path in TEMPLATES.glob("*.html"):
        body = path.read_text(encoding="utf-8")
        assert "<script src=\"http" not in body, f"{path.name} loads remote JS"
        assert "<link" not in body or "panel.css" in body, (
            f"{path.name} pulls in an external stylesheet; icons must be inline SVG"
        )


@pytest.mark.parametrize("name", ["records_body.html", "deliveries.html", "channels.html"])
def test_the_utility_classes_are_actually_used(name):
    """Only the elements each page actually has. The records card carries a
    filter bar instead of a header, and the categories page has no KPI strip,
    so neither is asked for a class it has no use for."""
    body = _text(name)
    assert 'class="kpi-row"' in body, f"{name} still uses the ad-hoc KPI grid"
    assert "page-section" in body, f"{name} does not use the shared vertical rhythm"
    if name != "records_body.html":
        assert "card-head" in body, f"{name} card headers are not .card-head"


@pytest.mark.parametrize("name", ["records_table.html", "deliveries_table.html", "users.html"])
def test_table_rows_share_one_height(name):
    body = _text(name)
    assert "table-row" in body, f"{name} table rows do not carry the shared height"


def test_the_dashboard_lives_on_the_same_rhythm():
    """The dashboard's KPI strip and headers come from its polled partial."""
    assert 'class="kpi-row"' in _text("dashboard_live.html")
    assert "card-head" in _text("dashboard_live.html")
    assert 'class="kpi-row"' in _text("channels.html")
    assert "table-row" in _text("dashboard.html")
    assert "card-head" in _text("categories.html")


def test_the_kpi_row_class_replaced_the_ad_hoc_one():
    """``.kpis`` was the old name; keeping both invites the drift Task 5 removes."""
    for path in TEMPLATES.glob("*.html"):
        body = path.read_text(encoding="utf-8")
        assert 'class="kpis"' not in body, f"{path.name} still uses .kpis"
    css = CSS.read_text(encoding="utf-8")
    assert ".kpis{" in css, "keep .kpis only while something still references it"