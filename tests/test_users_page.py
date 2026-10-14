"""One settings block for all users, and deleted users leave the panel (items 7/16).

Two owner complaints:

* every account added a fresh pair of sections to the users page (a role form
  and a password form), so the page got unusable with many users — the fix is
  ONE block where you select a user and then act;
* a deleted user still showed up in the panel — the fix is a tombstone that
  every listing and the login path filter out.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelUser, Role
from app.services import users as user_service
from tests.panel_test_client import panel_client


async def _user(session: AsyncSession, username: str, role_name: str = "admin") -> PanelUser:
    role = (await session.execute(select(Role).where(Role.name == role_name))).scalar_one_or_none()
    if role is None:
        role = Role(name=role_name, description=role_name)
        session.add(role)
        await session.flush()
    row = PanelUser(username=username, password_hash=user_service.hash_password("secret123"),
                    role_id=role.id, is_active=True)
    session.add(row)
    await session.commit()
    return row


async def _login(client, username: str = "u-admin"):
    response = await client.post(
        "/login", data={"username": username, "password": "secret123"}, follow_redirects=False
    )
    assert response.status_code == 302, f"login failed for {username}"
    # 302 is also what a REJECTED login returns, so prove the session works.
    probe = await client.get("/users", follow_redirects=False)
    assert probe.status_code == 200, f"{username} could not log in"


# --------------------------------------------------------------------------
# the single settings block
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_has_one_settings_block_not_one_per_user(session: AsyncSession):
    """The per-user loop is gone: forms do not scale with the account count."""
    await _user(session, "u-admin")
    await _user(session, "u-one", "editor")
    await _user(session, "u-two", "viewer")
    await _user(session, "u-three", "editor")
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/users")
    # The per-user loop is gone. The table keeps one compact toggle+delete pair
    # per ROW; what must not scale is the stacked settings sections, so the
    # page holds exactly one role form and one password form in total.
    assert page.text.count("/role\"") + page.text.count("/role ") >= 1
    assert page.text.count("/role") == 1, f"one role form, got {page.text.count('/role')}"
    assert page.text.count("/password") == 1, f"one password form, got {page.text.count('/password')}"
    assert "تنظیمات کاربر" in page.text
    for username in ("u-one", "u-two", "u-three"):
        assert username in page.text, "every user is still listed"
    # Four accounts, but the settings block is rendered once, for one of them.
    assert page.text.count("ثبت نقش") == 1
    assert page.text.count("ثبت گذرواژه") == 1


@pytest.mark.asyncio
async def test_selecting_a_user_switches_the_settings_block(session: AsyncSession):
    """Pick someone, get their block. No new section is added per user."""
    await _user(session, "u-admin")
    one = await _user(session, "u-one", "editor")
    await _user(session, "u-two", "viewer")
    async with panel_client() as client:
        await _login(client)
        default_page = await client.get("/users")
        assert f"/users/{one.id}/role" not in default_page.text, (
            "the default block is for the first user, not every user"
        )
        picked = await client.get(f"/users?user_id={one.id}")
    assert f"/users/{one.id}/role" in picked.text
    assert f"/users/{one.id}/password" in picked.text
    # Still exactly one block, even with someone selected.
    assert picked.text.count("ثبت نقش") == 1
    assert picked.text.count("ثبت گذرواژه") == 1


@pytest.mark.asyncio
async def test_role_can_be_changed_from_the_block(session: AsyncSession):
    await _user(session, "u-admin")
    target = await _user(session, "u-one", "viewer")
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            f"/users/{target.id}/role", data={"role": "editor"}, follow_redirects=False
        )
        assert response.status_code == 302
    await session.refresh(target)
    role = await session.get(Role, target.role_id)
    assert role.name == "editor"


@pytest.mark.asyncio
async def test_password_can_be_changed_from_the_block(session: AsyncSession):
    from app.services.passwords import verify_password

    await _user(session, "u-admin")
    target = await _user(session, "u-one", "editor")
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            f"/users/{target.id}/password", data={"new_password": "brand-new-99"},
            follow_redirects=False,
        )
        assert response.status_code == 302
    await session.refresh(target)
    assert verify_password(target.password_hash, "brand-new-99")


@pytest.mark.asyncio
async def test_unknown_user_id_falls_back_without_error(session: AsyncSession):
    await _user(session, "u-admin")
    await _user(session, "u-one", "editor")
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/users?user_id=999999")
    assert page.status_code == 200
    assert "تنظیمات کاربر" in page.text


# --------------------------------------------------------------------------
# deletion
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_user_is_gone_from_the_list_and_cannot_log_in(session: AsyncSession):
    """The owner's requirement, end to end through the panel."""
    admin = await _user(session, "u-admin")
    victim = await _user(session, "u-victim", "editor")
    async with panel_client() as client:
        await _login(client)
        assert "u-victim" in (await client.get("/users")).text
        deleted = await client.post(f"/users/{victim.id}/delete", follow_redirects=False)
        assert deleted.status_code == 302
        page = await client.get("/users")
    assert "u-victim" not in page.text, "a deleted user must not appear in the panel"

    async with panel_client() as client:
        login = await client.post(
            "/login", data={"username": "u-victim", "password": "secret123"},
            follow_redirects=False,
        )
        assert login.status_code == 401, "a deleted account cannot log in"

    # The row survives so the audit trail keeps its author.
    await session.refresh(victim)
    assert victim.deleted_at is not None
    assert victim.is_active is False


@pytest.mark.asyncio
async def test_delete_twice_is_harmless(session: AsyncSession):
    await _user(session, "u-admin")
    victim = await _user(session, "u-victim", "editor")
    async with panel_client() as client:
        await _login(client)
        first = await client.post(f"/users/{victim.id}/delete", follow_redirects=False)
        second = await client.post(f"/users/{victim.id}/delete", follow_redirects=False)
    assert first.status_code == 302
    assert second.status_code == 302, "a repeated delete must not error"


@pytest.mark.asyncio
async def test_safeguards_still_hold(session: AsyncSession):
    """Self-delete and last-admin are still refused."""
    admin = await _user(session, "u-admin")
    other = await _user(session, "u-other", "admin")
    async with panel_client() as client:
        await _login(client)
        assert (await client.post(f"/users/{admin.id}/delete", follow_redirects=False)).status_code == 422
        # Two admins exist, so deleting the other one is allowed.
        assert (await client.post(f"/users/{other.id}/delete", follow_redirects=False)).status_code == 302
        # Now the acting admin is the last one and must survive.
        assert (await client.post(f"/users/{admin.id}/delete", follow_redirects=False)).status_code == 422
    await session.refresh(admin)
    assert admin.deleted_at is None


@pytest.mark.asyncio
async def test_listing_excludes_deleted_accounts(session: AsyncSession):
    await _user(session, "u-admin")
    keeper = await _user(session, "u-keeper", "editor")
    gone = await _user(session, "u-gone", "editor")
    await user_service.delete_user(session, gone, actor_id=keeper.id)
    listed = {u["username"] for u in await user_service.list_users(session)}
    assert "u-keeper" in listed
    assert "u-gone" not in listed
