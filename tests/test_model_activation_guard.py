"""A model must be verified before it can be activated (owner-visible failure).

An unverified row sent to the provider produced a runtime

    status_code: 404, model_name: <row name>, body: Requested resource <name>
    does not exist

which the owner only ever saw as a failed news generation. Activation now
requires a recorded successful probe, so the mistake is caught at the settings
page with a Persian explanation.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Model, PanelUser, Role
from app.panel.app import panel_app
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession, username: str = "act-admin") -> None:
    role = (await session.execute(__import__("sqlalchemy").select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _model(session: AsyncSession, name: str, *, tested: bool | None) -> Model:
    from datetime import datetime

    row = Model(
        name=name, provider="openai", version="v1", is_active=False,
        last_tested_at=datetime.utcnow() if tested is not None else None,
        last_test_ok=tested,
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_untested_model_cannot_be_activated(session: AsyncSession):
    """Never-probed row: activation is refused with an explanation."""
    await _admin(session)
    row = await _model(session, "never-probed", tested=None)
    async with panel_client() as client:
        await client.post("/login", data={"username": "act-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(f"/models/{row.id}/activate", follow_redirects=False)
        assert response.status_code == 409
        assert "آزموده" in response.text
        # The message names the real cause (the provider's 404), in Persian digits.
        assert "۴۰۴" in response.text or "404" in response.text
    session.expire_all()
    await session.refresh(row)
    assert row.is_active is False


@pytest.mark.asyncio
async def test_failed_test_cannot_be_activated(session: AsyncSession):
    """A row whose probe FAILED must not become the active model."""
    await _admin(session)
    row = await _model(session, "failed-probe", tested=False)
    async with panel_client() as client:
        await client.post("/login", data={"username": "act-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(f"/models/{row.id}/activate", follow_redirects=False)
        assert response.status_code == 409
    session.expire_all()
    await session.refresh(row)
    assert row.is_active is False


@pytest.mark.asyncio
async def test_tested_model_can_be_activated(session: AsyncSession):
    """A row with a recorded successful probe activates normally."""
    await _admin(session)
    row = await _model(session, "tested-ok", tested=True)
    async with panel_client() as client:
        await client.post("/login", data={"username": "act-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(f"/models/{row.id}/activate", follow_redirects=False)
        assert response.status_code == 302
    session.expire_all()
    await session.refresh(row)
    assert row.is_active is True
    actions = (await session.execute(
        __import__("sqlalchemy").select(AuditLog.action).where(AuditLog.action == "model_activate")
    )).scalars().all()
    assert actions == ["model_activate"]


@pytest.mark.asyncio
async def test_activate_never_sends_a_row_name_as_the_provider_model(session: AsyncSession, monkeypatch):
    """The provider must never be called with the panel's internal label.

    This is the failure the owner hit: the row's display name was sent as the
    model id and the gateway answered 404.
    """
    from app.news import generation as gen
    from sqlalchemy import select as sel

    await _admin(session)
    row = Model(
        name="My Favourite Model", provider="openai", version="v1", is_active=True,
        api_model="qwen-plus",  # the id the provider actually knows
    )
    session.add(row)
    await session.commit()

    captured = {}

    class _FakeModel:
        def __init__(self, name, provider=None):
            captured["id"] = name

    class _FakeAgent:
        def __init__(self, model, output_type=None, system_prompt=None, model_settings=None):
            pass

        async def run(self, message):
            class _Out:
                title = "t"
                lead = "l"
                body = "b"

            return type("R", (), {"output": _Out()})()

    monkeypatch.setattr(gen, "OpenAIChatModel", _FakeModel)
    monkeypatch.setattr(gen, "Agent", _FakeAgent)
    monkeypatch.setattr(gen, "OpenAIProvider", lambda openai_client=None: None)
    monkeypatch.setattr(gen, "AsyncOpenAI", lambda **kwargs: object())

    from app.worker.worker import get_active_model

    active = await get_active_model(session)
    assert active is not None
    config = gen.resolve_generation_config(active, None, active, None)
    await gen.generate_news({"title": "x"}, **config)

    assert captured["id"] == "qwen-plus", (
        "the provider receives models.api_model, never the panel's display name"
    )
    assert captured["id"] != "My Favourite Model"