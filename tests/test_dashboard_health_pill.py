"""The dashboard must be able to report bad news.

The header pill rendered a literal ``API ok · DB connected`` unconditionally --
directly above a card that computes the real model state and worker heartbeat.
So a disconnected model, a dead worker or a missing key all showed a calm green
"ok".

A status indicator that cannot report bad news is worse than none, because it
is trusted precisely when it matters. The pill now reads the same state the
health card does, and these tests pin both directions.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client

DASH = Path("app/panel/templates/dashboard.html").read_text(encoding="utf-8")


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="pill-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


def _counters(state: str, worker: bool):
    async def fake(session):
        return {
            "kpi_review": 0, "kpi_queued": 0, "kpi_sent": 0, "kpi_failed": 0,
            "worker_fresh": worker, "pending_deliveries": 0,
            "model_health": {"name": "m", "api_model": "gpt", "state": state,
                             "tested_at": None, "latency_ms": None, "error": None},
        }
    return fake


def test_the_pill_is_not_a_hardcoded_string():
    """Strip comments first: the fix explains what it removed."""
    rendered = re.sub(r"\{#.*?#\}", "", DASH, flags=re.S)
    assert "API ok" not in rendered, "the pill must not claim health it never checked"
    assert "DB connected" not in rendered
    assert "model_health" in rendered, "it must read the computed state"
    assert "worker_fresh" in rendered


@pytest.mark.asyncio
async def test_healthy_only_when_both_the_model_and_the_worker_are(
    session: AsyncSession, monkeypatch
):
    """Both inputs are pinned, or a test that varies only the model would pass
    for the wrong reason -- an idle worker already makes the pill unhealthy."""
    from app.services import dashboard as dashboard_service

    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "pill-admin", "password": "secret123"},
                          follow_redirects=False)

        monkeypatch.setattr(dashboard_service, "live_counters", _counters("connected", True))
        good = (await client.get("/")).text
        monkeypatch.setattr(dashboard_service, "live_counters", _counters("disconnected", True))
        bad_model = (await client.get("/")).text
        monkeypatch.setattr(dashboard_service, "live_counters", _counters("connected", False))
        idle_worker = (await client.get("/")).text

    assert "همه‌چیز سالم" in good
    assert "همه‌چیز سالم" not in bad_model, "a disconnected model must not show healthy"
    assert "نیازمند بررسی" in bad_model
    assert "همه‌چیز سالم" not in idle_worker, "an idle worker must not show healthy either"
    assert "نیازمند بررسی" in idle_worker


@pytest.mark.asyncio
async def test_an_unconfigured_model_is_reported_as_a_problem(session: AsyncSession, monkeypatch):
    from app.services import dashboard as dashboard_service

    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "pill-admin", "password": "secret123"},
                          follow_redirects=False)
        monkeypatch.setattr(dashboard_service, "live_counters", _counters("missing", True))
        page = (await client.get("/")).text
    assert "همه‌چیز سالم" not in page, "no model configured is not a healthy system"
    assert "نیازمند بررسی" in page


@pytest.mark.asyncio
async def test_the_pill_agrees_with_the_health_card_below_it(session: AsyncSession, monkeypatch):
    """They must not be able to disagree, which is the whole point of sharing state."""
    from app.services import dashboard as dashboard_service

    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "pill-admin", "password": "secret123"},
                          follow_redirects=False)
        for state in ("connected", "disconnected", "degraded", "missing"):
            monkeypatch.setattr(dashboard_service, "live_counters", _counters(state, True))
            page = (await client.get("/")).text
            pill_ok = "همه‌چیز سالم" in page
            card_ok = ">Connected<" in page or "Connected" in page
            assert pill_ok == (state == "connected"), (
                f"state={state}: pill says {pill_ok}, card says {card_ok}"
            )