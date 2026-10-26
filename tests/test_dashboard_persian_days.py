"""The dashboard chart must label its days in Persian.

It labelled them "Friday", "Saturday", "Sunday"... on a page written entirely in
Persian. jdatetime does expose a locale (``set_locale("fa")``), but
``strftime("%A")`` ignores it and keeps returning English — so the fix is an
explicit mapping rather than a locale switch, and this pins that it stays.

The subtle part is the index. ``jdatetime``'s ``weekday()`` counts the Iranian
week, Saturday first (0) and Friday last (6). Python's own ``date.weekday()``
counts Monday first. Using the Python convention here would silently shift every
label by one — the chart would read correctly on average and be wrong on
exactly the days a reader checks.
"""

from datetime import date, timedelta

import pytest

from app.panel.app import _FA_WEEKDAYS, _fa_weekday

# 2026-10-17 is a Saturday, so this range covers a whole Iranian week.
SATURDAY = date(2026, 10, 17)


def test_the_seven_names_are_iranian_days_starting_saturday():
    assert _FA_WEEKDAYS == ("شنبه", "یک‌شنبه", "دوشنبه", "سه‌شنبه",
                            "چهارشنبه", "پنج‌شنبه", "جمعه")
    assert len(_FA_WEEKDAYS) == 7
    assert len(set(_FA_WEEKDAYS)) == 7


@pytest.mark.parametrize("offset,expected", [
    (0, "شنبه"), (1, "یک‌شنبه"), (2, "دوشنبه"), (3, "سه‌شنبه"),
    (4, "چهارشنبه"), (5, "پنج‌شنبه"), (6, "جمعه"),
])
def test_each_day_maps_to_the_right_name(offset, expected):
    assert _fa_weekday(SATURDAY + timedelta(days=offset)) == expected


def test_it_is_not_off_by_one_from_the_persian_week():
    """The bug this guards: ISO Monday-first indexing shifts every label."""
    import jdatetime

    for offset in range(7):
        day = SATURDAY + timedelta(days=offset)
        assert jdatetime.date.fromgregorian(date=day).weekday() == offset, (
            "jdatetime.weekday() is expected to be Saturday-first"
        )
        assert _fa_weekday(day) == _FA_WEEKDAYS[offset]


def test_no_label_ever_comes_back_in_english():
    for offset in range(14):
        name = _fa_weekday(SATURDAY + timedelta(days=offset))
        assert name.isascii() is False, f"{name!r} is not Persian"


def test_a_bad_value_degrades_rather_than_crashing_a_chart_axis():
    """A chart axis is the worst possible place for a 500."""
    assert _fa_weekday(None)
    assert _fa_weekday("not-a-date")


@pytest.mark.asyncio
async def test_the_dashboard_renders_persian_days(session):
    from sqlalchemy import select

    from app.db.models import PanelUser, Role
    from app.services.passwords import hash_password
    from tests.panel_test_client import panel_client

    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="day-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()

    async with panel_client() as client:
        await client.post("/login", data={"username": "day-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/")
    assert page.status_code == 200
    for english in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday"):
        assert english not in page.text, f"the chart still says {english}"
    # Seven bars, each labelled in Persian.
    for name in _FA_WEEKDAYS:
        assert name in page.text, f"{name} missing from the chart"
    assert page.text.count("<div style=\"height:") >= 7


@pytest.mark.asyncio
async def test_the_live_poll_labels_days_in_persian_too(session):
    """The chart is outside the polled region, but the helper is shared."""
    from sqlalchemy import select

    from app.db.models import PanelUser, Role
    from app.services.passwords import hash_password
    from tests.panel_test_client import panel_client

    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="day2-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()

    async with panel_client() as client:
        await client.post("/login", data={"username": "day2-admin", "password": "secret123"},
                          follow_redirects=False)
        partial = await client.get("/dashboard/live")
    assert partial.status_code == 200
    for english in ("Monday", "Friday", "Sunday"):
        assert english not in partial.text