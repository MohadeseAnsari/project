"""Delivery domain service: listing, retry/release transitions, counts.

Single implementation shared by the machine API and the panel. Raises plain
``ValueError`` with stable messages; routes map them to HTTP statuses or
error pages.
"""

from datetime import datetime

from sqlalchemy import func, select

from app.db.models import Channel, Delivery, Main
from app.services.transitions import assert_delivery_transition


def delivery_filters(
    status: str | None = None,
    platform: str | None = None,
    main_id: int | None = None,
) -> list:
    """Build the shared WHERE clauses for delivery listings.

    ``platform`` targets the joined channel row, so callers join
    ``Channel`` whenever a platform filter is present (both do).
    """
    filters = []
    if status:
        filters.append(Delivery.status == status)
    if platform:
        filters.append(Channel.platform == platform)
    if main_id is not None:
        filters.append(Delivery.main_id == main_id)
    return filters


async def fetch_deliveries(
    session,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    platform: str | None = None,
    main_id: int | None = None,
) -> tuple[list, int]:
    """Return ``((delivery, channel, article) rows, filtered total)``."""
    base = select(func.count()).select_from(Delivery)
    if platform:
        base = base.join(Channel, Channel.id == Delivery.channel_id)
    filters = delivery_filters(status, platform, main_id)
    if filters:
        base = base.where(*filters)
    total = (await session.execute(base)).scalar_one()

    query = (
        select(Delivery, Channel, Main)
        .join(Channel, Channel.id == Delivery.channel_id)
        .join(Main, Main.id == Delivery.main_id)
    )
    if filters:
        query = query.where(*filters)
    pairs = (
        await session.execute(
            query.order_by(Delivery.id.desc()).limit(page_size).offset((page - 1) * page_size)
        )
    ).all()
    return list(pairs), total


async def get_delivery(session, delivery_id: int) -> Delivery | None:
    """Fetch one delivery by id (both layers 404 the same way)."""
    result = await session.execute(select(Delivery).where(Delivery.id == delivery_id))
    return result.scalar_one_or_none()


def delivery_rows(pairs) -> list[dict]:
    """Shape ``(delivery, channel, article)`` triples for list templates.

    One builder for the full page and the HTMX partial so both show the same
    columns. Category names are attached by :func:`attach_category_names`
    (kept out of this pure shape step so it stays trivially testable).
    """
    return [
        {
            "id": d.id, "main_id": d.main_id, "status": d.status, "attempts": d.attempts,
            "platform_message_id": d.platform_message_id, "title": m.title, "field": m.field,
            "channel_name": c.username or c.title or c.platform,
            "category_id": d.category_id, "route_source": d.route_source,
            "category_name": "—",
        }
        for d, c, m in pairs
    ]


async def attach_category_names(session, items: list[dict]) -> list[dict]:
    """Fill ``category_name`` on shaped delivery rows (routing provenance)."""
    from app.db.models import Category

    ids = [item["category_id"] for item in items if item["category_id"]]
    if ids:
        cats = (
            await session.execute(select(Category).where(Category.id.in_(ids)))
        ).scalars().all()
        names = {c.id: c.name for c in cats}
        for item in items:
            if item["category_id"]:
                item["category_name"] = names.get(item["category_id"], "—")
    return items


async def delivery_items(session, pairs) -> list[dict]:
    """Shape + enrich delivery rows for the panel tables (one shared builder)."""
    return await attach_category_names(session, delivery_rows(pairs))


async def last_failed(session) -> Delivery | None:
    """The most recent failed delivery (for the page's last-error note)."""
    return (
        await session.execute(
            select(Delivery).where(Delivery.status == "failed").order_by(Delivery.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


def delivery_info(delivery, channel) -> dict:
    """Shape one delivery + its channel into the machine-API payload.

    Shared by ``POST /deliveries/{id}/retry`` and ``.../release`` so both
    endpoints return the identical contract, and neither route needs to load
    the channel row itself.
    """
    return {
        "id": delivery.id,
        "channel_id": delivery.channel_id,
        "main_id": delivery.main_id,
        "platform": channel.platform if channel else None,
        "username": channel.username if channel else None,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "error_message": delivery.error_message,
        "sent_at": delivery.sent_at,
    }


async def delivery_with_channel(session, delivery_id: int) -> tuple[Delivery, Channel] | None:
    """Fetch a delivery together with its channel, or None when unknown."""
    delivery = await get_delivery(session, delivery_id)
    if delivery is None:
        return None
    return delivery, await session.get(Channel, delivery.channel_id)


async def retry_delivery(session, delivery: Delivery) -> Delivery:
    """Re-queue a ``failed`` delivery as ``pending`` (attempts reset).

    Raises:
        ValueError: ``not-retryable`` for any other status, so a send in
            flight or an already-sent article is never duplicated.
    """
    from datetime import datetime

    if delivery.status != "failed":
        raise ValueError("not-retryable")
    delivery.status = "pending"
    delivery.attempts = 0
    delivery.claimed_at = None
    delivery.error_message = None
    delivery.next_attempt_at = datetime.utcnow()
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(delivery)
    return delivery


async def release_delivery(session, delivery: Delivery) -> Delivery:
    """Release one ``awaiting_confirmation`` delivery as ``pending``.

    Unlike retry, attempts are kept: this is the delivery's first release,
    not a re-queue after failure.

    Raises:
        ValueError: ``not-awaiting`` for any other status.
    """
    from datetime import datetime

    if delivery.status != "awaiting_confirmation":
        raise ValueError("not-awaiting")
    delivery.status = "pending"
    delivery.claimed_at = None
    delivery.next_attempt_at = datetime.utcnow()
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(delivery)
    return delivery


async def cancel_delivery(session, delivery: Delivery) -> Delivery:
    """Cancel a delivery that has not been sent yet.

    The recovery action for a delivery the operator no longer wants to send
    (wrong channel, duplicate queue, article rejected later). Only rows that
    have NOT left for the platform can be cancelled — ``sending`` is already
    in flight, and ``sent`` cannot be un-sent (use
    :func:`mark_deleted_from_channel` for that).

    Raises:
        ValueError: ``not-cancellable`` for anything else.
    """
    from datetime import datetime

    if delivery.status not in ("awaiting_confirmation", "pending", "failed"):
        raise ValueError("not-cancellable")
    assert_delivery_transition(delivery.status, "failed")
    delivery.status = "failed"
    delivery.claimed_at = None
    delivery.error_message = (delivery.error_message or "") + (
        "" if not delivery.error_message else " | "
    ) + "cancelled by operator"
    delivery.next_attempt_at = datetime.utcnow()
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    return delivery


async def delete_published_message(session, delivery: Delivery) -> Delivery:
    """Delete the published message AT THE PLATFORM, then record it (PART B).

    The real removal the owner asked for: it calls the channel's own
    ``deleteMessage`` so the article disappears from the channel, and only
    after the platform confirms does the row move to
    ``deleted_from_channel`` (the message id stays — the row is the history).

    The platform error is translated into a code here, so callers never
    import the messaging layer and the operator sees one message per cause.
    A refused delete leaves the row ``sent``: the message is still there, and
    a retry is meaningful.

    Raises:
        ValueError: ``not-sent`` when nothing was published,
            ``missing-message-id`` when the row has no platform message id,
            ``channel-not-found`` when the destination row is gone,
            ``no-token`` when the channel has no usable bot token,
            ``delete-refused`` when the platform will not delete it
            (too old, wrong chat, no rights) — retrying cannot help,
            ``delete-unavailable`` on a transport/server failure.
    """
    from app.db.models import Channel as _Channel
    from app.messaging.platforms import (
        DeliveryError,
        PermanentDeliveryError,
        client_for_channel,
        configured_clients,
    )

    if delivery.status != "sent":
        raise ValueError("not-sent")
    if not delivery.platform_message_id:
        raise ValueError("missing-message-id")
    channel = await session.get(_Channel, delivery.channel_id)
    if channel is None:
        raise ValueError("channel-not-found")

    # The channel's OWN token wins (so a channel added from the panel deletes
    # without any environment change); otherwise the platform-wide token — the
    # same one that published the message, and the only one allowed to.
    client, token_error = client_for_channel(channel, configured_clients())
    if client is None:
        # The reason names the platform or the variable, never a token value.
        raise ValueError(f"no-token: {token_error}")

    try:
        await client.delete_message(channel.chat_id, delivery.platform_message_id)
    except PermanentDeliveryError as e:
        raise ValueError(f"delete-refused: {e}")
    except DeliveryError as e:
        raise ValueError(f"delete-unavailable: {e}")

    assert_delivery_transition("sent", "deleted_from_channel")
    delivery.status = "deleted_from_channel"
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    return delivery


async def mark_deleted_from_channel(session, delivery: Delivery) -> Delivery:
    """Record ``deleted_from_channel`` WITHOUT calling the platform.

    The fallback when the message was removed by hand in the channel app: the
    operator says so and the row reflects it. Use
    :func:`delete_published_message` to have the panel delete it for you.

    Raises:
        ValueError: ``not-sent`` when the message was never published.
    """
    if delivery.status != "sent":
        raise ValueError("not-sent")
    assert_delivery_transition("sent", "deleted_from_channel")
    delivery.status = "deleted_from_channel"
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    return delivery


async def delivery_status_counts(session) -> dict[str, int]:
    """Delivery counts per status (stats endpoint, dashboard, gate notes)."""
    rows = (await session.execute(select(Delivery.status, func.count()).group_by(Delivery.status))).all()
    return {status: count for status, count in rows}
