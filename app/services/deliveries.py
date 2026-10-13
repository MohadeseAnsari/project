"""Delivery domain service: listing, retry/release transitions, counts.

Single implementation shared by the machine API and the panel. Raises plain
``ValueError`` with stable messages; routes map them to HTTP statuses or
error pages.
"""

from sqlalchemy import func, select

from app.db.models import Channel, Delivery, Main


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


async def delivery_status_counts(session) -> dict[str, int]:
    """Delivery counts per status (stats endpoint, dashboard, gate notes)."""
    rows = (await session.execute(select(Delivery.status, func.count()).group_by(Delivery.status))).all()
    return {status: count for status, count in rows}
