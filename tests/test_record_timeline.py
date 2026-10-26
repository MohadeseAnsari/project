"""Record flow timeline and category reassignment (E3).

The timeline must be built from real columns and audit rows — it is what
explains to an operator why a record looks the way it does.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.connection import async_session
from app.db.models import (
    AuditLog,
    Category,
    Channel,
    Delivery,
    Field,
    Main,
    Model,
    PanelUser,
    Prompt,
)
from app.services import records as record_service
from app.services.passwords import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession, username: str = "timeline-admin") -> None:
    from app.db.models import PanelUser, Role

    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username=username, password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _full_article(session: AsyncSession, *, delivery_status: str = "sent") -> Main:
    """An article that went all the way through: classified, generated, approved, sent."""
    from datetime import datetime

    category = Category(name="timeline-cat", description="test")
    other = Category(name="timeline-other", description="other")
    session.add_all([category, other])
    await session.commit()
    field = Field(name="timeline-field", normalized_name="timeline-field")
    model = Model(name="timeline-model", provider="openai", version="v1", is_active=False)
    prompt = Prompt(version="timeline-v1", system_prompt="P", is_active=False)
    session.add_all([field, model, prompt])
    await session.commit()
    row = Main(
        username="t", title="Timeline", abstract="A", field="timeline-field", university="U",
        author="A", supervisor="S", status="published", field_id=field.id,
        category_id=category.id, model_id=model.id, prompt_id=prompt.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
        claimed_at=datetime.utcnow(), approved_at=datetime.utcnow(),
        published_at=datetime.utcnow(),
    )
    session.add(row)
    await session.commit()
    channel = Channel(platform="bale", chat_id=9920001, username="@tl", is_active=True)
    session.add(channel)
    await session.commit()
    session.add(Delivery(
        main_id=row.id, channel_id=channel.id, status=delivery_status,
        platform_message_id=777 if delivery_status == "sent" else None,
        sent_at=datetime.utcnow() if delivery_status == "sent" else None,
    ))
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_timeline_covers_the_whole_flow(session: AsyncSession):
    """submitted -> field/category -> generated with model/prompt -> delivered."""
    await _admin(session)
    row = await _full_article(session)

    steps = await record_service.record_timeline(session, row)
    titles = [s["title"] for s in steps]
    assert "ثبت درخواست" in titles
    assert any("تعیین دسته" in t for t in titles)
    assert any("تولید خروجی" in t for t in titles)

    category_step = next(s for s in steps if "تعیین دسته" in s["title"])
    assert "timeline-field" in category_step["sub"]
    assert "timeline-cat" in category_step["sub"]

    generation = next(s for s in steps if "تولید خروجی" in s["title"])
    assert "timeline-model" in generation["sub"]
    assert "timeline-v1" in generation["sub"]

    sent = next(s for s in steps if s["title"].startswith("ارسال به"))
    assert "777" in sent["sub"], "the channel message id is shown"


@pytest.mark.asyncio
async def test_timeline_shows_review_actions_with_who_and_when(session: AsyncSession):
    """Human actions appear in the timeline with the actor's name."""
    await _admin(session)
    row = await _full_article(session, delivery_status="pending")
    actor = (await session.execute(select(PanelUser))).scalars().one()
    session.add(AuditLog(user_id=actor.id, action="approve", main_id=row.id, detail="تأیید و انتشار"))
    session.add(AuditLog(user_id=actor.id, action="reject", main_id=row.id, detail="رد رکورد"))
    await session.commit()

    steps = await record_service.record_timeline(session, row)
    labels = [s["title"] for s in steps]
    assert "تأیید بازبین" in labels
    assert any("timeline-admin" in s["sub"] for s in steps)
    approve = next(s for s in steps if s["title"] == "تأیید بازبین")
    assert approve["at"] is not None


@pytest.mark.asyncio
async def test_timeline_reports_failure_and_unsent_states(session: AsyncSession):
    """A failed send is shown as failed, not as delivered."""
    await _admin(session)
    row = await _full_article(session, delivery_status="failed")
    steps = await record_service.record_timeline(session, row)
    assert any("ارسال ناموفق" in s["title"] for s in steps)
    assert not any(s["title"].startswith("ارسال به") for s in steps)


@pytest.mark.asyncio
async def test_reassign_category_over_http(session: AsyncSession):
    """The recovery action re-points one record and audits it."""
    await _admin(session)
    row = await _full_article(session, delivery_status="pending")
    wrong = (
        await session.execute(select(Category).where(Category.name == "timeline-other"))
    ).scalar_one()

    async with panel_client() as client:
        await client.post("/login", data={"username": "timeline-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get(f"/records/{row.id}")
        assert "اصلاح دستهٔ رکورد" in page.text
        assert "جریان این رکورد" in page.text

        response = await client.post(
            f"/records/{row.id}/category", data={"category_id": wrong.id}, follow_redirects=False
        )
        assert response.status_code == 302

    # Re-fetch in a fresh session: the panel used its own request-scoped one.
    async with async_session() as check:
        stored = await check.get(Main, row.id)
        assert stored.category_id == wrong.id
    actions = (
        await session.execute(
            select(AuditLog.action).where(AuditLog.main_id == row.id, AuditLog.action == "reassign_category")
        )
    ).scalars().all()
    assert actions == ["reassign_category"]


@pytest.mark.asyncio
async def test_reassign_unknown_category_is_422(session: AsyncSession):
    """An unknown target returns a Persian message, never a 500."""
    await _admin(session)
    row = await _full_article(session, delivery_status="pending")
    async with panel_client() as client:
        await client.post("/login", data={"username": "timeline-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            f"/records/{row.id}/category", data={"category_id": "999999"}, follow_redirects=False
        )
        assert response.status_code == 422
        assert "دستهٔ انتخاب‌شده وجود ندارد" in response.text


@pytest.mark.asyncio
async def test_timeline_renders_on_every_lifecycle_state(session: AsyncSession):
    """The panel page renders its timeline for each state, never a traceback."""
    await _admin(session)
    row = await _full_article(session, delivery_status="pending")
    async with panel_client() as client:
        await client.post("/login", data={"username": "timeline-admin", "password": "secret123"},
                          follow_redirects=False)
        for status in ("ready", "processing", "review", "approved", "published", "rejected", "failed"):
            await session.execute(
                Main.__table__.update().where(Main.id == row.id).values(status=status)
            )
            await session.commit()
            page = await client.get(f"/records/{row.id}")
            assert page.status_code == 200, status
            assert "جریان این رکورد" in page.text, status
            assert "Traceback" not in page.text, status