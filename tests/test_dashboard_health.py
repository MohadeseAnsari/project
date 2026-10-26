"""Dashboard health: the active model's real connection state (item 1).

The console must answer "is the thing that writes my news reachable right
now?" without guessing. The state is computed in the service layer so the
dashboard and any other consumer cannot disagree, and it must never leak the
API key.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Channel, Field, Main, PanelUser, Role
from app.panel.auth import hash_password
from app.services import models as model_service
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession, username: str = "dash-admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


def _model(session: AsyncSession, **kwargs):
    from app.db.models import Model


    defaults = dict(name="gpt", provider="openai", version="1", is_active=True)
    defaults.update(kwargs)
    row = Model(**defaults)
    session.add(row)
    return row


@pytest.mark.asyncio
async def test_no_model_reports_not_configured(session: AsyncSession):
    """An empty console must say so, not crash or imply a working model."""
    health = await model_service.active_model_health(session)
    assert health["state"] == "missing"
    assert health["name"] is None


@pytest.mark.asyncio
async def test_configured_but_never_probed_reports_checking(session: AsyncSession, monkeypatch):
    """No probe yet means "Checking", never a false "Connected"."""
    from app.config import settings

    monkeypatch.setattr(settings, "avalai_api_key", "env-key")
    _model(session, last_test_ok=None)
    await session.commit()
    health = await model_service.active_model_health(session)
    assert health["state"] == "untested"
    assert health["tested_at"] is None


@pytest.mark.asyncio
async def test_successful_probe_reports_connected(session: AsyncSession, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "avalai_api_key", "env-key")
    _model(session, last_test_ok=True, last_tested_at=datetime.utcnow(),
           last_test_latency_ms=420)
    await session.commit()
    health = await model_service.active_model_health(session)
    assert health["state"] == "connected"
    assert health["latency_ms"] == 420
    assert health["tested_at"] is not None
    assert health["error"] is None


@pytest.mark.asyncio
async def test_failed_probe_reports_disconnected_with_the_reason(session: AsyncSession, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "avalai_api_key", "env-key")
    _model(session, last_test_ok=False, last_test_error="HTTP 401: bad key")
    await session.commit()
    health = await model_service.active_model_health(session)
    assert health["state"] == "disconnected"
    assert "401" in health["error"]


@pytest.mark.asyncio
async def test_only_the_active_model_is_reported(session: AsyncSession, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "avalai_api_key", "env-key")
    _model(session, name="old", is_active=False, last_test_ok=False, last_test_error="stale")
    _model(session, name="live", is_active=True, last_test_ok=True)
    await session.commit()
    health = await model_service.active_model_health(session)
    assert health["name"] == "live"
    assert health["state"] == "connected"


@pytest.mark.asyncio
async def test_health_never_exposes_the_key(session: AsyncSession, monkeypatch):
    """Whatever the state, no credential value comes back."""
    from app.config import settings
    from app.credentials import encrypt

    monkeypatch.setattr(settings, "avalai_api_key", "")
    _model(session, api_key_encrypted=encrypt("sk-SUPER-SECRET-KEY"))
    await session.commit()
    health = await model_service.active_model_health(session)
    rendered = repr(health)
    assert "SUPER-SECRET-KEY" not in rendered
    assert "sk-" not in rendered
    assert health["has_credential"] is True


# --------------------------------------------------------------------------
# the rendered page
# --------------------------------------------------------------------------


async def _seed(session: AsyncSession) -> None:
    category = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(category)
    await session.commit()
    session.add(Field(name="f", normalized_name="f"))
    session.add(Channel(platform="bale", chat_id=90001, is_active=True))
    await session.commit()
    session.add(Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                     author="A", supervisor="S", status="review"))
    await session.commit()


@pytest.mark.asyncio
async def test_dashboard_shows_the_active_model_row(session: AsyncSession, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "avalai_api_key", "env-key")
    await _admin(session)
    await _seed(session)
    _model(session, name="live-model", last_test_ok=True,
           last_tested_at=datetime.utcnow() - timedelta(minutes=1), last_test_latency_ms=310)
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/")
    assert page.status_code == 200
    assert "مدل فعال" in page.text
    assert "live-model" in page.text
    assert "Connected" in page.text


@pytest.mark.asyncio
async def test_dashboard_no_longer_says_no_recent_heartbeat(session: AsyncSession):
    """The Persian heartbeat phrase is gone from the health card."""
    await _admin(session)
    await _seed(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/")
    assert "بدون ضربان اخیر" not in page.text
    assert "ضربان تازه" not in page.text
    # A meaningful English word instead, for both worker states.
    assert "Idle" in page.text or "Active" in page.text


@pytest.mark.asyncio
async def test_dashboard_says_checking_when_never_probed(session: AsyncSession, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "avalai_api_key", "env-key")
    await _admin(session)
    await _seed(session)
    _model(session, name="fresh", last_test_ok=None)
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "dash-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/")
    assert "Checking" in page.text
    assert "هنوز بررسی نشده" in page.text
