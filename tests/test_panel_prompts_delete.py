"""Panel: deleting a prompt version from the /prompts page (trash icon)."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Main, PanelUser, Prompt, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


async def _user(session: AsyncSession, username: str = "panel-admin", role_name: str = "admin") -> None:
    role = (await session.execute(select(Role).where(Role.name == role_name))).scalar_one_or_none()
    if role is None:
        role = Role(name=role_name, description=role_name)
        session.add(role)
        await session.flush()
    session.add(
        PanelUser(
            username=username,
            password_hash=hash_password("secret123"),
            role_id=role.id,
            is_active=True,
        )
    )
    await session.commit()


async def _login(client, username: str = "panel-admin") -> None:
    response = await client.post(
        "/login", data={"username": username, "password": "secret123"},
        follow_redirects=False,
    )
    assert response.status_code == 302


async def _prompt(session: AsyncSession, version: str, *, active: bool = False) -> Prompt:
    row = Prompt(version=version, system_prompt="prompt text", is_active=active)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_prompt_delete_icon_appears_only_for_inactive_versions(session: AsyncSession):
    active = await _prompt(session, "v-active", active=True)
    old = await _prompt(session, "v-old")
    await _user(session)

    async with panel_client() as client:
        await _login(client)
        page = await client.get("/prompts")

    assert page.status_code == 200
    assert f'action="/prompts/{old.id}/delete"' in page.text
    assert f'action="/prompts/{active.id}/delete"' not in page.text


@pytest.mark.asyncio
async def test_prompt_delete_removes_unused_inactive_version(session: AsyncSession):
    await _prompt(session, "v-active", active=True)
    old = await _prompt(session, "v-old")
    await _user(session)

    async with panel_client() as client:
        await _login(client)
        response = await client.post(f"/prompts/{old.id}/delete", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/prompts"
    remaining = (
        await session.execute(select(Prompt.id).where(Prompt.id == old.id))
    ).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_prompt_delete_refuses_active_version(session: AsyncSession):
    active = await _prompt(session, "v-active", active=True)
    await _user(session)

    async with panel_client() as client:
        await _login(client)
        response = await client.post(f"/prompts/{active.id}/delete")

    assert response.status_code == 422
    assert "پرامپت فعال قابل حذف نیست" in response.text
    assert await session.get(Prompt, active.id) is not None


@pytest.mark.asyncio
async def test_prompt_delete_refuses_version_used_by_a_record(session: AsyncSession):
    await _prompt(session, "v-active", active=True)
    used = await _prompt(session, "v-used")
    session.add(
        Main(
            username="student",
            title="title",
            abstract="abstract",
            field="field",
            university="university",
            author="author",
            supervisor="supervisor",
            status="ready",
            prompt_id=used.id,
        )
    )
    await session.commit()
    await _user(session)

    async with panel_client() as client:
        await _login(client)
        response = await client.post(f"/prompts/{used.id}/delete")

    assert response.status_code == 422
    assert "برای حفظ تاریخچه حذف نمی‌شود" in response.text
    assert await session.get(Prompt, used.id) is not None


@pytest.mark.asyncio
async def test_prompt_delete_is_admin_only(session: AsyncSession):
    await _prompt(session, "v-active", active=True)
    old = await _prompt(session, "v-old")
    await _user(session, "panel-editor", "editor")

    async with panel_client() as client:
        await _login(client, "panel-editor")
        response = await client.post(f"/prompts/{old.id}/delete")

    assert response.status_code == 403
    assert await session.get(Prompt, old.id) is not None
