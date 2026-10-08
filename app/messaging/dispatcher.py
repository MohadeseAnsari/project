"""Delivery queue logic: what still needs sending, claiming it, recording outcomes.

The unit of work is a ``Delivery`` — one (article, channel) pair. Two functions
answer the two halves of the loop:

- ``ensure_pending_deliveries`` turns *processed* articles into pending sends.
  A processed article is a ``main`` row in ``review`` (generation finished,
  ``output_json`` populated). It is not a flag anywhere: "processed but not
  sent" is defined by the absence of a ``sent``/``failed`` delivery for each
  routed channel.
- ``claim_next_delivery`` takes the oldest due delivery and marks it
  ``sending``, so the send happens at most once per attempt even if several
  sender replicas are ever run.

Claiming uses ``FOR UPDATE SKIP LOCKED``, mirroring ``app.worker.worker``, and
sends are retried with exponential backoff up to ``sender_max_attempts``.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Channel, Delivery, FieldChannelRoute, Main
from app.messaging.formatting import render_article
from app.messaging.platforms import DeliveryError, PlatformClient

logger = logging.getLogger(__name__)

# Error text is stored for manual inspection; provider responses are already
# truncated by the clients, and this keeps a pathological message from bloating
# the row.
_MAX_ERROR_LENGTH = 2000


def _backoff_delay(attempts: int) -> float:
    """Seconds to wait before attempt ``attempts + 1``.

    Exponential from ``sender_backoff_base_seconds`` (10s, 20s, 40s, 80s...), so
    a provider outage is not hammered while a one-off failure is still retried
    within the same minute.
    """
    return settings.sender_backoff_base_seconds * (2 ** max(0, attempts - 1))


async def channels_for_article(session, article: Main) -> list[Channel]:
    """Return the active channels this article must be published to.

    Resolution order:
    1. The article's field routes (``field_channel_routes``).
    2. If the field has no route — or has none to an *active* channel — every
       active channel. This is a deliberate fallback rather than a silent drop:
       fields created at runtime by the generation Worker (unknown fields) have
       no route row, and losing their articles would be worse than publishing
       them to the current channel set.
    """
    routed: list[Channel] = []
    if article.field_id is not None:
        result = await session.execute(
            select(Channel)
            .join(FieldChannelRoute, FieldChannelRoute.channel_id == Channel.id)
            .where(FieldChannelRoute.field_id == article.field_id)
            .where(Channel.is_active.is_(True))
            .order_by(Channel.id)
        )
        routed = list(result.scalars().all())

    if routed:
        return routed

    result = await session.execute(
        select(Channel).where(Channel.is_active.is_(True)).order_by(Channel.id)
    )
    return list(result.scalars().all())


def _has_undelivered_channel():
    """SQL predicate: the article resolves to some active channel with no delivery.

    Mirrors ``channels_for_article`` exactly, because the two must agree or the
    batch query would either skip articles the resolver would route (silent
    non-delivery) or keep returning articles the resolver has nothing to do
    for (the starvation this replaces).

    An active channel ``c`` is a target when either:

    - the article's field routes to ``c``, or
    - the field has no route to any *active* channel, in which case every
      active channel is a target (the documented fallback).

    ``c`` still needs sending when no ``deliveries`` row exists for
    ``(article, c)``. Note this is per-channel, not per-article: an article
    already sent to one channel but not another must still be returned.
    """
    # Does this field route to ANY active channel? Distinguishes "routed
    # somewhere" from "has no active route", which is what makes the fallback
    # fire in `channels_for_article`.
    #
    # `.correlate(Main)` is required, not decorative: without it SQLAlchemy adds
    # `main` to this subquery's own FROM and renders
    # `field_channel_routes.field_id = main.field_id` as a self-comparison that
    # is true for any field with any route, making the predicate global rather
    # than per-article.
    field_has_active_route = exists(
        select(1)
        .select_from(FieldChannelRoute)
        .join(Channel, Channel.id == FieldChannelRoute.channel_id)
        .where(
            and_(
                FieldChannelRoute.field_id == Main.field_id,
                Channel.is_active.is_(True),
            )
        )
        .correlate(Main)
    )

    # One row per active channel this article could go to, LEFT JOINed to its
    # route (is_routed) and to its delivery (delivered). Kept as a single join
    # level rather than nested EXISTS on an aliased Channel: a nested
    # correlated subquery re-renders its own `channels_1` alias instead of
    # binding the outer one, which silently turns a per-channel test into a
    # per-article one. `.correlate(Main)` then keeps the outer article bound.
    return exists(
        select(1)
        .select_from(Channel)
        .outerjoin(
            FieldChannelRoute,
            and_(
                FieldChannelRoute.field_id == Main.field_id,
                FieldChannelRoute.channel_id == Channel.id,
            ),
        )
        .outerjoin(
            Delivery,
            and_(
                Delivery.main_id == Main.id,
                Delivery.channel_id == Channel.id,
            ),
        )
        .where(
            and_(
                Channel.is_active.is_(True),
                # No delivery row for (this article, this channel).
                Delivery.id.is_(None),
                # Either the field routes to this channel, or it routes to no
                # active channel at all and the fallback applies.
                or_(
                    FieldChannelRoute.channel_id.isnot(None),
                    ~field_has_active_route,
                ),
            )
        )
        .correlate(Main)
    )


async def ensure_pending_deliveries(session, batch_size: int = 500) -> int:
    """Create pending deliveries for processed articles that lack them.

    An article qualifies when it is ``review`` with a usable ``output_json`` AND
    at least one channel it resolves to has no delivery row yet.

    The "still needs sending" test is an anti-join expressed in SQL, not a
    Python filter applied after ``LIMIT``. That distinction is load-bearing: if
    the exclusion ran afterwards, the first ``batch_size`` articles would fill
    the window with rows that are already queued, the query would return the
    same window on every subsequent pass, and any article beyond it would never
    be delivered — permanently, not slowly, because the window stays saturated
    as the queue drains. Keeping the exclusion inside the query means the
    ``LIMIT`` only ever counts articles that have real work to do.

    Human gate: when ``settings.require_human_confirmation`` is True (default),
    new rows are created as ``awaiting_confirmation`` so nothing publishes
    without explicit approval; approving flips them to ``pending``. When False,
    rows are created as ``pending`` directly (the pre-gate behaviour). The gate
    only changes the initial status — the anti-join/LIMIT selection above is
    unchanged.

    The per-article ``already`` set below is kept as a race guard for a second
    sender replica inserting between the SELECT and the INSERT; the unique
    constraint on ``(main_id, channel_id)`` is the final backstop.

    Returns:
        Number of deliveries created.
    """
    result = await session.execute(
        select(Main)
        .where(Main.status == "review")
        .where(Main.output_json.isnot(None))
        .where(_has_undelivered_channel())
        .order_by(Main.id)
        .limit(batch_size)
    )
    articles = list(result.scalars().all())
    if not articles:
        return 0

    created = 0
    for article in articles:
        existing = await session.execute(
            select(Delivery.channel_id).where(Delivery.main_id == article.id)
        )
        already = set(existing.scalars().all())

        for channel in await channels_for_article(session, article):
            if channel.id in already:
                continue
            if not render_article(article.output_json, channel.username):
                # Nothing renderable (empty output_json): record nothing, so a
                # later corrected article can still be delivered.
                logger.warning(
                    "Skipping article id=%s: no renderable content for channel id=%s",
                    article.id,
                    channel.id,
                )
                continue
            session.add(
                Delivery(
                    main_id=article.id,
                    channel_id=channel.id,
                    status=(
                        "awaiting_confirmation"
                        if settings.require_human_confirmation
                        else "pending"
                    ),
                    attempts=0,
                )
            )
            created += 1

    if created:
        await session.commit()
        logger.info("Queued %d pending deliveries", created)
    return created


async def confirm_deliveries_for_article(session, article_id: int) -> int:
    """Flip an article's ``awaiting_confirmation`` deliveries to ``pending``.

    Called once when a reviewer approves the article. Only the gated status
    moves — ``sent``/``failed``/``pending`` rows are never touched — so
    approving twice is a no-op rather than a duplicate send.

    Returns:
        Number of deliveries flipped.
    """
    result = await session.execute(
        update(Delivery)
        .where(Delivery.main_id == article_id)
        .where(Delivery.status == "awaiting_confirmation")
        .values(status="pending", updated_at=datetime.utcnow())
    )
    await session.commit()
    if result.rowcount:
        logger.info(
            "Confirmed %d deliveries for article id=%s", result.rowcount, article_id
        )
    return result.rowcount or 0


async def recover_stale_deliveries(session) -> int:
    """Return deliveries abandoned in ``sending`` back to ``pending``.

    A send attempt is normally seconds. A ``sending`` row whose ``claimed_at``
    is older than ``sender_lease_minutes`` means the sender Worker died between
    claiming and recording the outcome; re-queueing it is the same at-least-once
    trade-off the generation Worker already makes (a duplicate post in the
    channel in exchange for never losing an article).
    """
    cutoff = datetime.utcnow() - timedelta(minutes=settings.sender_lease_minutes)
    result = await session.execute(
        update(Delivery)
        .where(Delivery.status == "sending")
        .where(Delivery.claimed_at < cutoff)
        .values(status="pending", claimed_at=None, next_attempt_at=datetime.utcnow())
    )
    await session.commit()
    if result.rowcount:
        logger.warning("Recovered %d stale deliveries", result.rowcount)
    return result.rowcount


async def claim_next_delivery(session) -> Delivery | None:
    """Claim the oldest due ``pending`` delivery (FIFO by ``created_at``).

    "Due" means ``next_attempt_at`` has passed, which is what makes backoff work:
    a failed delivery is parked in the future instead of being retried
    immediately.

    Returns:
        The delivery with status ``sending`` and ``attempts`` incremented, or
        ``None`` when nothing is due.
    """
    now = datetime.utcnow()
    result = await session.execute(
        select(Delivery)
        .where(Delivery.status == "pending")
        .where(Delivery.next_attempt_at <= now)
        .order_by(Delivery.created_at, Delivery.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    delivery = result.scalar_one_or_none()
    if delivery:
        delivery.status = "sending"
        delivery.attempts += 1
        delivery.claimed_at = now
        await session.commit()
        logger.info(
            "Claimed delivery id=%s article=%s channel=%s attempt=%s",
            delivery.id,
            delivery.main_id,
            delivery.channel_id,
            delivery.attempts,
        )
    return delivery


async def load_delivery_context(session, delivery: Delivery) -> tuple[Main, Channel] | None:
    """Load the article and channel for a claimed delivery.

    Returns ``None`` when either side no longer exists (an operator deleted the
    channel); the caller then abandons the delivery rather than retrying a send
    that can never succeed.
    """
    result = await session.execute(
        select(Delivery)
        .where(Delivery.id == delivery.id)
        .options(selectinload(Delivery.article), selectinload(Delivery.channel))
    )
    loaded = result.scalar_one_or_none()
    if loaded is None or loaded.article is None or loaded.channel is None:
        return None
    return loaded.article, loaded.channel


async def mark_sent(session, delivery: Delivery, platform_message_id: int) -> None:
    """Record a successful send."""
    delivery.status = "sent"
    delivery.sent_at = datetime.utcnow()
    delivery.platform_message_id = platform_message_id
    delivery.claimed_at = None
    delivery.error_message = None
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    logger.info(
        "Delivered id=%s article=%s channel=%s message_id=%s",
        delivery.id,
        delivery.main_id,
        delivery.channel_id,
        platform_message_id,
    )


async def mark_retry(session, delivery: Delivery, error: str, delay_seconds: float) -> None:
    """Return a failed delivery to ``pending``, scheduled after ``delay_seconds``.

    Keeps the attempt count so the backoff grows and the attempt cap can be
    enforced by the caller.
    """
    delivery.status = "pending"
    delivery.claimed_at = None
    delivery.error_message = error[:_MAX_ERROR_LENGTH]
    delivery.next_attempt_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    logger.warning(
        "Send failed (will retry) delivery id=%s attempt=%s in %.0fs error=%s",
        delivery.id,
        delivery.attempts,
        delay_seconds,
        error,
    )


async def mark_failed(session, delivery: Delivery, error: str) -> None:
    """Park a delivery in ``failed`` with its error, for manual inspection."""
    delivery.status = "failed"
    delivery.claimed_at = None
    delivery.error_message = error[:_MAX_ERROR_LENGTH]
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    logger.error("Send gave up delivery id=%s attempts=%s error=%s", delivery.id, delivery.attempts, error)


async def deliver_one(
    session,
    delivery: Delivery,
    clients: dict[str, PlatformClient],
) -> bool:
    """Attempt one claimed delivery. Returns ``True`` when it was sent.

    Failure policy (the decision, in one place):
    - success → ``sent``
    - retryable error and attempts remaining → ``pending`` after backoff
    - retryable error with no attempts left, or a permanent error → ``failed``

    A permanent error short-circuits the attempt cap on purpose: an unknown
    ``chat_id`` will still be unknown in ten minutes.
    """
    context = await load_delivery_context(session, delivery)
    if context is None:
        await mark_failed(session, delivery, "article or channel no longer exists")
        return False
    article, channel = context

    text = render_article(article.output_json, channel.username)
    if not text:
        await mark_failed(session, delivery, "article has no renderable content")
        return False

    client = clients.get(channel.platform)
    if client is None:
        await mark_failed(
            session, delivery, f"no bot token configured for platform '{channel.platform}'"
        )
        return False

    try:
        message_id = await client.send_text(channel.chat_id, text)
    except DeliveryError as e:
        exhausted = delivery.attempts >= settings.sender_max_attempts
        if not e.retryable or exhausted:
            await mark_failed(session, delivery, str(e))
            return False
        await mark_retry(session, delivery, str(e), _backoff_delay(delivery.attempts))
        return False
    except asyncio.CancelledError:
        # Shutdown mid-send: leave the row in `sending` and let stale-delivery
        # recovery re-queue it, rather than recording a failure that never happened.
        raise
    except Exception as e:  # defensive: an unexpected bug must not kill the loop
        # The attempt cap applies here too. A bug that throws on every attempt
        # would otherwise retry forever, which is the unbounded-retry failure
        # mode the cap exists to prevent -- and one bad article would sit in the
        # queue re-raising instead of being parked for inspection.
        error = f"unexpected error: {e}"
        if delivery.attempts >= settings.sender_max_attempts:
            await mark_failed(session, delivery, error)
        else:
            await mark_retry(session, delivery, error, _backoff_delay(delivery.attempts))
        return False

    await mark_sent(session, delivery, message_id)
    return True
