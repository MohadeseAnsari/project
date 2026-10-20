"""End-to-end proof: panel submit -> worker -> approve -> sender -> sent.

One sequence through the real components sharing the single database:
the panel inserts the job, the generation Worker processes it, the human gate
holds the delivery, panel approval releases it, and the sender Worker delivers
it. The LLM call and the platform HTTP call are the only fakes; every state
transition, query, and audit row is real.
"""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import AuditLog, Category, Channel, Delivery, Main
from app.messaging.dispatcher import claim_next_delivery, deliver_one, ensure_pending_deliveries
from app.panel.app import panel_app
from app.panel.auth import hash_password
from app.worker.worker import claim_next_job, process_job
from tests.panel_test_client import panel_client
from tests.routing_setup import route_all_active


class StubClient:
    """Fake platform client: records sends, always succeeds."""

    def __init__(self):
        self.calls: list[tuple[int, str]] = []

    async def send_text(self, chat_id: int, text: str) -> int:
        self.calls.append((chat_id, text))
        return 4242


async def _admin(session: AsyncSession) -> None:
    from app.db.models import PanelUser, Role

    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(
        PanelUser(username="e2e-admin", password_hash=hash_password("secret123"),
                  role_id=role.id, is_active=True)
    )
    session.add(Category(name="other", description="Other"))
    session.add(Category(name="بهداشت", description="health"))
    session.add(Channel(platform="bale", chat_id=98001, username="@c", is_active=True))
    await session.commit()


def _panel():
    """CSRF-compliant panel client (browser simulation; see panel_test_client)."""
    return panel_client()


@pytest.mark.asyncio
async def test_panel_to_channel_full_flow(session: AsyncSession, monkeypatch):
    """Submit in panel, generate, approve, send: every handoff verified."""
    from app.main import app as api_app

    monkeypatch.setattr(settings, "require_human_confirmation", True)
    await _admin(session)

    async with _panel() as client:
        login = await client.post(
            "/login", data={"username": "e2e-admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert login.status_code == 302

        submitted = await client.post(
            "/submit",
            data={
                "username": "e2e", "title": "E2E Thesis", "abstract": "Abstract",
                "field": "پزشکی", "university": "Tehran University",
                "table_of_contents": "1. Intro", "author": "Author", "supervisor": "Supervisor",
            },
            follow_redirects=False,
        )
        assert submitted.status_code == 302
        record_id = int(submitted.headers["location"].rsplit("/", 1)[-1])

        row = await session.get(Main, record_id)
        assert row.status == "ready"

        # Generation Worker picks it up on its next poll (LLM call faked).
        # Claim first, exactly like app.worker.worker.run_worker does: the
        # state machine only allows processing -> review.
        claimed = await claim_next_job(session)
        assert claimed is not None and claimed.id == record_id
        from app.db.models import MainCategoryMatch
        from app.schemas.news import CategoryMatch, GeneratedNews

        # A real qualifying match: a fallback-only record queues NO deliveries
        # by design, and this test verifies the delivery handoff, not the gate.
        with patch("app.worker.worker.generate_news") as mock_gen:
            mock_gen.return_value = GeneratedNews(
                title="T", lead="L", body="B",
                categories=[CategoryMatch(category="بهداشت", score=0.9, reason="پزشکی است")],
            )
            await process_job(session, claimed)
        await session.refresh(row)
        assert row.status == "review"
        assert row.output_json == {"title": "T", "lead": "L", "body": "B"}

        # Delivery routes through the STORED MATCH, not category_id: the
        # combined call's classification is stored above and the display
        # primary points at the top match.
        matches = (await session.execute(
            select(MainCategoryMatch).where(MainCategoryMatch.main_id == record_id)
        )).scalars().all()
        assert len(matches) == 1, "the combined call must store its match"
        assert row.category_id == matches[0].category_id

        # The category is routed to every active channel; routing itself is
        # covered elsewhere.
        assert row.category_id is not None
        await route_all_active(session, await session.get(Category, row.category_id))

        # Sender queues it, but the human gate holds it back.
        assert await ensure_pending_deliveries(session) == 1
        delivery = (await session.execute(select(Delivery))).scalars().one()
        assert delivery.status == "awaiting_confirmation"
        assert await claim_next_delivery(session) is None

        # Human approval in the panel releases it to pending.
        approved = await client.post(
            f"/records/{record_id}/review",
            data={"action": "approve", "title": "", "lead": "", "body": ""},
            follow_redirects=False,
        )
        assert approved.status_code == 302
        await session.refresh(delivery)
        assert delivery.status == "pending"

        # Sender Worker takes it and delivers it to the channel.
        claimed = await claim_next_delivery(session)
        assert claimed is not None and claimed.id == delivery.id
        stub = StubClient()
        assert await deliver_one(session, claimed, {"bale": stub}) is True
        await session.refresh(delivery)
        assert delivery.status == "sent"
        assert delivery.platform_message_id == 4242
        assert len(stub.calls) == 1

        actions = sorted(
            (
                await session.execute(select(AuditLog.action).where(AuditLog.main_id == record_id))
            ).scalars().all()
        )
        assert actions == ["approve", "submit"]

    # The machine API is untouched: the IranDoc client reads the same record,
    # and now sees the true lifecycle state: approved + delivered = published.
    from httpx import AsyncClient as HttpxClient

    async with HttpxClient(transport=ASGITransport(app=api_app), base_url="http://test") as api:
        response = await api.get(
            f"/records/{record_id}", params={"shared_Secret": settings.shared_secret}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "published"
        assert response.json()["approved_at"] is not None
        assert response.json()["published_at"] is not None
