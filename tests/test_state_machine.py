"""The one state machine (E1): records and deliveries.

Every status write in the app goes through
``app.services.transitions``; these tests pin the allowed graphs and prove
that the panel, the API and the Workers all honour them.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.transitions import (
    DELIVERY_TRANSITIONS,
    RECORD_TRANSITIONS,
    STATUS_FA,
    InvalidTransition,
    assert_delivery_transition,
    assert_record_transition,
)


@pytest.mark.parametrize(
    "current,new,domain",
    [
        ("ready", "processing", "record"),
        ("processing", "review", "record"),
        ("processing", "failed", "record"),
        ("processing", "ready", "record"),  # stale-job recovery
        ("review", "approved", "record"),
        ("review", "rejected", "record"),
        ("review", "failed", "record"),  # regenerate error
        ("review", "published", "record"),  # human gate OFF: first send publishes
        ("approved", "published", "record"),
        ("failed", "review", "record"),  # regenerate
        ("awaiting_confirmation", "pending", "delivery"),
        ("pending", "sending", "delivery"),
        ("sending", "sent", "delivery"),
        ("sending", "failed", "delivery"),
        ("sending", "pending", "delivery"),  # retry / recovery
        ("failed", "pending", "delivery"),  # manual retry
        ("sent", "deleted_from_channel", "delivery"),
    ],
)
def test_allowed_transitions(current, new, domain):
    if domain == "record":
        assert_record_transition(current, new)
    else:
        assert_delivery_transition(current, new)


@pytest.mark.parametrize(
    "current,new",
    [
        ("ready", "review"),  # cannot skip generation
        ("ready", "approved"),
        ("review", "processing"),
        ("rejected", "review"),  # terminal: a rejection is final
        ("rejected", "approved"),
        ("published", "review"),
        ("approved", "rejected"),
    ],
)
def test_forbidden_record_transitions(current, new):
    with pytest.raises(InvalidTransition):
        assert_record_transition(current, new)


@pytest.mark.parametrize(
    "current,new",
    [
        ("awaiting_confirmation", "sending"),  # gate must be released first
        ("pending", "sent"),
        ("sent", "pending"),
        ("deleted_from_channel", "pending"),
    ],
)
def test_forbidden_delivery_transitions(current, new):
    with pytest.raises(InvalidTransition):
        assert_delivery_transition(current, new)


def test_terminal_states_have_no_exits():
    for terminal in ("published", "rejected"):
        assert RECORD_TRANSITIONS[terminal] == set()
    assert DELIVERY_TRANSITIONS["deleted_from_channel"] == set()


def test_every_record_status_has_a_persian_label():
    for status in RECORD_TRANSITIONS:
        assert status in STATUS_FA, f"no Persian label for record status {status}"


@pytest.mark.asyncio
async def test_service_review_uses_declared_transitions(session: AsyncSession):
    """apply_review only performs transitions the machine declares."""
    from app.db.models import Main
    from app.services import records as record_service

    row = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()

    # review -> approved is declared.
    assert_record_transition("review", "approved")
    await record_service.apply_review(session, row, action="approve")
    await session.refresh(row)
    assert row.status == "approved"

    # A second approve is refused by the status guard (not a silent re-write).
    with pytest.raises(ValueError, match="not-in-review"):
        await record_service.apply_review(session, row, action="approve")


@pytest.mark.asyncio
async def test_illegal_transition_is_refused_by_service(session: AsyncSession):
    """A row outside review cannot be approved, whatever the caller wants."""
    from app.db.models import Main
    from app.services import records as record_service

    row = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="rejected",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    with pytest.raises(ValueError, match="not-in-review"):
        await record_service.apply_review(session, row, action="approve")
    await session.refresh(row)
    assert row.status == "rejected"


@pytest.mark.asyncio
async def test_needs_review_count_is_exact(session: AsyncSession):
    """The needs-review KPI counts only 'review' rows."""
    from app.db.models import Main
    from app.services import records as record_service

    rows = [
        Main(username="t", title=str(i), abstract="A", field="f", university="U",
             author="A", supervisor="S", status=status,
             output_json={"title": "T", "lead": "L", "body": "B"})
        for i, status in enumerate(
            ["review", "review", "approved", "published", "rejected", "failed", "ready"]
        )
    ]
    session.add_all(rows)
    await session.commit()
    assert await record_service.needs_review_count(session) == 2

    counts = await record_service.record_status_counts(session)
    assert counts["approved"] == 1 and counts["published"] == 1
    assert counts["rejected"] == 1 and counts["failed"] == 1


@pytest.mark.asyncio
async def test_delivery_retry_and_release_use_the_machine(session: AsyncSession):
    """Delivery services refuse transitions outside DELIVERY_TRANSITIONS."""
    from app.db.models import Channel, Delivery, Main
    from app.services import deliveries as delivery_service

    row = Main(
        username="t", title="T", abstract="A", field="f", university="U",
        author="A", supervisor="S", status="approved",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    channel = Channel(platform="bale", chat_id=77001, username="@c")
    session.add(channel)
    await session.commit()

    failed = Delivery(main_id=row.id, channel_id=channel.id, status="failed", attempts=3)
    session.add(failed)
    await session.commit()

    # failed -> pending is allowed (manual retry)
    assert_delivery_transition(failed.status, "pending")
    await delivery_service.retry_delivery(session, failed)
    await session.refresh(failed)
    assert failed.status == "pending" and failed.attempts == 0

    # awaiting_confirmation is release-only
    with pytest.raises(ValueError, match="not-awaiting"):
        await delivery_service.release_delivery(session, failed)

    # a sent row is not retryable (never duplicate a published post).
    # Use a second channel: deliveries are unique per (main_id, channel_id).
    other_channel = Channel(platform="telegram", chat_id=77002, username="@d")
    session.add(other_channel)
    await session.commit()
    sent_row = Delivery(main_id=row.id, channel_id=other_channel.id, status="sent")
    session.add(sent_row)
    await session.commit()
    with pytest.raises(ValueError, match="not-retryable"):
        await delivery_service.retry_delivery(session, sent_row)