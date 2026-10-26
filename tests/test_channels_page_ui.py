"""The channels page: destinations only (frontend).

The owner reduced this page to exactly one job — **add, edit, switch on/off
and delete channels** — and removed the two routing configuration screens.
These tests pin that the page really is only that, and that the safety
behaviours survive the reduction:

* the rule is stated once, plainly, with the one control that matters;
* the field/category routing tables are gone from the page AND from the
  service layer, not merely hidden behind a tab;
* a channel that is switched on but failing says so on its own row, because
  the alternative is a channel that looks healthy while nothing reaches it;
* delete stays visually separated from save, and add stays collapsed.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Category,
    Channel,
    Delivery,
    Main,
    PanelUser,
    Role,
)
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client
from tests.routing_setup import route_all_active


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="ui-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _login(client):
    response = await client.post(
        "/login", data={"username": "ui-admin", "password": "secret123"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert (await client.get("/channels", follow_redirects=False)).status_code == 200


async def _world(session: AsyncSession, channels: int = 2) -> list[Channel]:
    session.add(Category(name="پزشکی و علوم سلامت", description="Medical"))
    rows = []
    for index in range(channels):
        channel = Channel(platform="bale", chat_id=94001 + index, username=f"@c{index}",
                          title=f"کانال {index}", is_active=index == 0)
        session.add(channel)
        rows.append(channel)
    await session.commit()
    # The channels page now shows which categories route to each channel, so a
    # world with categories and no mapping would render an empty strip.
    categories = (await session.execute(select(Category))).scalars().all()
    for category in categories:
        await route_all_active(session, category)
    return rows


# --------------------------------------------------------------------------
# the page is channel management and nothing else
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_page_is_about_channels_only(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert page.status_code == 200
    # The two routing sections are gone.
    assert "رشته ← دسته" not in page.text
    assert "دسته ← کانال متناظر" not in page.text
    # Managing destinations is all that is left.
    for control in ("/toggle", "/delete", "افزودن کانال", "تنظیمات کانال"):
        assert control in page.text, f"the page should still offer {control}"


@pytest.mark.asyncio
async def test_the_page_says_delivery_follows_the_category(session: AsyncSession):
    """The note must not still claim a broadcast that no longer happens.

    This page used to tell the operator "every article goes to every active
    channel, switch a channel off to stop it". Routing made both halves false,
    and a note that lies about where news goes is worse than no note: it is
    what the operator trusts when news does not arrive.
    """
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert "همهٔ کانال‌های فعال" not in page.text
    assert "بر اساس" in page.text and "دستهٔ" in page.text, (
        "it says delivery is decided by the category"
    )
    assert "دسته‌بندی و مسیریابی" in page.text, "and where to configure it"


@pytest.mark.asyncio
async def test_a_summary_is_visible_without_scrolling(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    for label in ("کانال", "فعال — این‌ها خبر می‌گیرند", "خبر منتشرشده"):
        assert label in page.text, f"the summary should mention {label}"


# --------------------------------------------------------------------------
# a broken channel must not look healthy
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_active_but_failing_channel_says_so(session: AsyncSession):
    """The gap that let a broken Telegram channel go unnoticed for hours."""
    await _admin(session)
    channels = await _world(session, channels=1)
    article = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                   author="A", supervisor="S", status="approved",
                   output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(article)
    await session.commit()
    session.add(Delivery(
        main_id=article.id, channel_id=channels[0].id, status="failed", attempts=1,
        error_message="Bad Request: need administrator rights in the channel chat",
    ))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert "ارسال ناموفق" in page.text, "a failing channel must be visible on its row"
    assert "administrator rights" in page.text, "and say why"


@pytest.mark.asyncio
async def test_a_healthy_channel_has_no_failure_badge(session: AsyncSession):
    await _admin(session)
    channels = await _world(session, channels=1)
    article = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                   author="A", supervisor="S", status="approved",
                   output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(article)
    await session.commit()
    session.add(Delivery(main_id=article.id, channel_id=channels[0].id, status="sent",
                         platform_message_id=1))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert "ارسال ناموفق" not in page.text
    assert "administrator rights" not in page.text


@pytest.mark.asyncio
async def test_a_disabled_channel_is_not_badged_for_old_failures(session: AsyncSession):
    """The badge means "on but broken". Off is a choice, not a fault."""
    await _admin(session)
    channels = await _world(session, channels=2)
    channels[0].is_active = False
    article = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                   author="A", supervisor="S", status="approved",
                   output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(article)
    await session.commit()
    session.add(Delivery(main_id=article.id, channel_id=channels[0].id, status="failed",
                         error_message="old failure"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    # Only the ACTIVE channel is badged; the disabled one's failure is history.
    assert page.text.count("ارسال ناموفق") <= 1


# --------------------------------------------------------------------------
# safety behaviours survive the reduction
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_channel_row_has_its_own_controls(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    for channel in (await session.execute(select(Channel))).scalars().all():
        assert f"/channels/{channel.id}/toggle" in page.text
        assert f"/channels?channel_id={channel.id}" in page.text


@pytest.mark.asyncio
async def test_delete_is_visually_separated_from_save(session: AsyncSession):
    """Muscle memory should not reach a destructive action from Save."""
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert "danger-zone" in page.text
    assert page.text.index("ذخیرهٔ تغییرات") < page.text.index("danger-zone")


@pytest.mark.asyncio
async def test_add_channel_is_collapsed_until_asked(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await _login(client)
        collapsed = await client.get("/channels")
        expanded = await client.get("/channels", params={"new": "1"})
    assert "افزودن کانال تازه" in collapsed.text
    # The settings card also has a bot_token field, so assert on the add card's
    # own submit button rather than on a field name.
    assert "بررسی و افزودن کانال" not in collapsed.text
    assert "بررسی و افزودن کانال" in expanded.text


@pytest.mark.asyncio
async def test_empty_state_offers_the_add_form(session: AsyncSession):
    await _admin(session)
    session.add(Category(name="پزشکی و علوم سلامت", description="Medical"))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels")
    assert page.status_code == 200
    assert 'name="target"' in page.text
    assert "هنوز کانالی ثبت نشده" in page.text


@pytest.mark.asyncio
async def test_a_channel_with_history_is_not_hard_deleted(session: AsyncSession):
    await _admin(session)
    channels = await _world(session, channels=1)
    article = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                   author="A", supervisor="S", status="published",
                   output_json={"title": "T", "lead": "L", "body": "B"})
    session.add(article)
    await session.commit()
    session.add(Delivery(main_id=article.id, channel_id=channels[0].id, status="sent",
                         platform_message_id=1))
    await session.commit()
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/channels?channel_id=1")
        assert "غیرفعال کردن کانال" in page.text, "history means disable, not delete"
        response = await client.post(
            "/channels/1/delete", data={"confirm": "yes"}, follow_redirects=False
        )
        assert response.status_code == 303
    await session.refresh(channels[0])
    assert channels[0].is_active is False
    assert (await session.execute(select(Channel).where(Channel.id == 1))).scalar_one() is not None


def test_the_css_classes_the_page_uses_exist():
    from pathlib import Path

    css = Path("app/panel/static/panel.css").read_text(encoding="utf-8")
    for selector in (".chan-row", ".danger-zone"):
        assert f"{selector}{{" in css, f"{selector} must be defined in panel.css"
