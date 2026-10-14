"""Channel domain service: destinations, settings, and route mappings.

Single implementation for the panel (the machine API has no channel
endpoints; ``scripts/manage_channels.py`` stays a thin CLI over the same
rows). Raises plain ``ValueError`` with stable messages; routes map them to
error pages. Bot tokens never pass through here — only variable names.
"""

from sqlalchemy import func, select

from app.db.models import (
    Category,
    CategoryChannelRoute,
    Channel,
    Field,
    FieldChannelRoute,
)


async def list_channels(session) -> list[Channel]:
    """All channels in id order (cards, selects, matrix headers)."""
    return list((await session.execute(select(Channel).order_by(Channel.id))).scalars().all())


async def get_channel(session, channel_id: int) -> Channel | None:
    """Fetch one channel by id (routes 404 the same way)."""
    return await session.get(Channel, channel_id)


async def channels_with_route_counts(session) -> list[Channel]:
    """Channels with ``route_count`` and ``sent_count`` attached.

    Two grouped queries for every channel rather than one per channel, so the
    dashboard does not fan out into N queries as channels are added.
    """
    from app.db.models import Delivery

    channels = await list_channels(session)
    route_counts = dict(
        (
            await session.execute(
                select(FieldChannelRoute.channel_id, func.count())
                .group_by(FieldChannelRoute.channel_id)
            )
        ).all()
    )
    sent_counts = dict(
        (
            await session.execute(
                select(Delivery.channel_id, func.count())
                .where(Delivery.status == "sent")
                .group_by(Delivery.channel_id)
            )
        ).all()
    )
    for channel in channels:
        channel.route_count = route_counts.get(channel.id, 0)
        channel.sent_count = sent_counts.get(channel.id, 0)
    return channels


async def resolve_target(platform: str, target: str, token: str | None = None) -> dict:
    """Validate a numeric id or @username live via getChat before saving.

    Returns the provider result (numeric ``id``, ``username``, ``title``).

    ``token`` lets the operator supply the bot token in the panel form, so a
    channel can be added with no environment change and no restart. Without it
    the platform-wide token is used.

    Raises:
        ValueError: ``bad-platform`` for unknown platforms,
            ``no-platform-token`` when there is no token to validate with,
            ``unresolvable: ...`` when getChat rejects the target,
            ``bad-result`` when the provider answer has no numeric id.
    """
    from app.messaging.platforms import DeliveryError, build_client, configured_clients

    if platform not in ("bale", "telegram"):
        raise ValueError("bad-platform")
    token = (token or "").strip()
    if token:
        client = build_client(platform, token)
    else:
        client = configured_clients().get(platform)
        if client is None:
            raise ValueError("no-platform-token")
    target = target.strip()
    chat: int | str = int(target) if target.lstrip("-").isdigit() else target
    try:
        resolved = await client.get_chat(chat)
    except DeliveryError as e:
        raise ValueError(f"unresolvable: {e}")
    try:
        resolved_id = int(resolved.get("id", 0))
    except (TypeError, ValueError):
        raise ValueError("bad-result")
    if not resolved_id:
        raise ValueError("bad-result")
    return {
        "chat_id": resolved_id,
        "username": resolved.get("username") or (target if isinstance(chat, str) else None),
        "title": resolved.get("title"),
    }


async def find_by_destination(session, platform: str, chat_id: int) -> Channel | None:
    """The channel already registered for this ``(platform, chat_id)``, if any.

    Checked BEFORE writing so a clash is a clear domain error instead of a
    failed commit — which would roll the session back and leave the panel
    unable to render even the error page.
    """
    return (
        await session.execute(
            select(Channel).where(Channel.platform == platform, Channel.chat_id == chat_id)
        )
    ).scalar_one_or_none()


async def create_channel(
    session,
    *,
    platform: str,
    target: str,
    title: str | None = None,
    bot_token_env: str | None = None,
    bot_token: str | None = None,
) -> Channel:
    """Validate the target live, then INSERT a new channel row.

    Channels are a LIST: this always adds a row, so a new destination joins
    the existing ones instead of replacing anything. Only an identical
    ``(platform, chat_id)`` is refused, as a duplicate.

    ``bot_token`` is this channel's own bot token. It is encrypted before
    storage (write-only, never returned) and used to validate the target, so
    one channel can be added entirely from the panel.

    Raises:
        ValueError: ``duplicate-channel`` when the (platform, chat_id) pair
            exists, plus the ``resolve_target`` failures above.
    """
    from app.credentials import encrypt

    resolved = await resolve_target(platform, target, token=bot_token)
    if await find_by_destination(session, platform, resolved["chat_id"]) is not None:
        raise ValueError("duplicate-channel")
    row = Channel(
        platform=platform,
        chat_id=resolved["chat_id"],
        username=resolved["username"],
        title=title.strip() or resolved["title"] if title else resolved["title"],
        bot_token_env=(bot_token_env or "").strip() or None,
        bot_token_encrypted=encrypt((bot_token or "").strip() or None),
        is_active=True,
    )
    session.add(row)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise ValueError("duplicate-channel")
    await session.refresh(row)
    return row


async def update_channel(
    session,
    row: Channel,
    updates: dict,
) -> Channel:
    """Update one channel's own fields; other channels are never touched.

    Only the fields in ``updates`` change, so editing channel A cannot
    overwrite channel B — the failure the owner hit when a second channel
    seemed to replace the first.

    Raises:
        ValueError: ``duplicate-channel`` when ``chat_id`` collides with
            another channel on the same platform. The clash is detected before
            anything is written, so the row keeps its old values.
    """
    allowed = {"title", "username", "chat_id", "bot_token_env", "is_active"}
    new_chat_id = updates.get("chat_id")
    if new_chat_id is not None and new_chat_id != row.chat_id:
        clash = await find_by_destination(session, row.platform, new_chat_id)
        if clash is not None and clash.id != row.id:
            raise ValueError("duplicate-channel")
    for field_name, value in updates.items():
        if field_name in allowed:
            setattr(row, field_name, value)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise ValueError("duplicate-channel")
    await session.refresh(row)
    return row


async def toggle_channel(session, row: Channel) -> Channel:
    """Flip a channel's active switch."""
    row.is_active = not row.is_active
    await session.commit()
    await session.refresh(row)
    return row


async def delete_channel(session, row: Channel) -> str:
    """Remove a channel, but never its published history.

    A channel that already has deliveries is **disabled instead of deleted**:
    those rows record which message went where, and the row they point at has
    to keep resolving. Deleting is only for a channel that never received
    anything; its field/category routes go first, so no route can dangle.

    Returns ``"deleted"`` or ``"disabled"``.
    """
    from app.db.models import Delivery

    deliveries = (
        await session.execute(
            select(func.count()).select_from(Delivery).where(Delivery.channel_id == row.id)
        )
    ).scalar_one()
    if deliveries:
        if row.is_active:
            row.is_active = False
            await session.commit()
            await session.refresh(row)
        return "disabled"

    await remove_routes(session, channel_id=row.id)
    await session.delete(row)
    await session.commit()
    return "deleted"


async def remove_routes(session, channel_id: int) -> int:
    """Drop every field/category route pointing at one channel.

    Runs before a delete so no route outlives the row it points at. Returns
    how many routes were removed (shown in the confirmation text).
    """
    field_rows = list(
        (
            await session.execute(
                select(FieldChannelRoute).where(FieldChannelRoute.channel_id == channel_id)
            )
        ).scalars().all()
    )
    for route in field_rows:
        await session.delete(route)
    cat_rows = list(
        (
            await session.execute(
                select(CategoryChannelRoute).where(CategoryChannelRoute.channel_id == channel_id)
            )
        ).scalars().all()
    )
    for route in cat_rows:
        await session.delete(route)
    if field_rows or cat_rows:
        await session.commit()
    return len(field_rows) + len(cat_rows)


async def set_channel_token(session, row: Channel, token: str | None) -> Channel:
    """Replace a channel's own bot token, encrypted at rest.

    An empty ``token`` KEEPS what is stored; clearing is the explicit
    ``clear_token`` flag, so an edit form submitted with a blank box does not
    silently unbreak a working channel.
    """
    from app.credentials import encrypt

    token = (token or "").strip()
    if token:
        row.bot_token_encrypted = encrypt(token)
        await session.commit()
        await session.refresh(row)
    return row


async def clear_channel_token(session, row: Channel) -> Channel:
    """Forget a channel's own bot token, falling back to the platform-wide one."""
    if row.bot_token_encrypted:
        row.bot_token_encrypted = None
        await session.commit()
        await session.refresh(row)
    return row


async def channel_usage(session) -> dict[int, dict[str, int]]:
    """Per-channel delivery and route counts, for the delete confirmation.

    One query per table instead of one per channel, so a page with many
    channels does not fan out into N queries.
    """
    from app.db.models import Delivery

    sent = dict(
        (
            await session.execute(
                select(Delivery.channel_id, func.count())
                .where(Delivery.status.in_(("sent", "deleted_from_channel")))
                .group_by(Delivery.channel_id)
            )
        ).all()
    )
    all_deliveries = dict(
        (
            await session.execute(
                select(Delivery.channel_id, func.count()).group_by(Delivery.channel_id)
            )
        ).all()
    )
    field_counts = dict(
        (
            await session.execute(
                select(FieldChannelRoute.channel_id, func.count())
                .group_by(FieldChannelRoute.channel_id)
            )
        ).all()
    )
    cat_counts = dict(
        (
            await session.execute(
                select(CategoryChannelRoute.channel_id, func.count())
                .group_by(CategoryChannelRoute.channel_id)
            )
        ).all()
    )
    ids = set(all_deliveries) | set(field_counts) | set(cat_counts)
    return {
        cid: {
            "deliveries": all_deliveries.get(cid, 0),
            "sent": sent.get(cid, 0),
            "field_routes": field_counts.get(cid, 0),
            "category_routes": cat_counts.get(cid, 0),
        }
        for cid in ids
    }


async def has_deliveries(session, channel_id: int) -> bool:
    """True when a channel already carries delivery history (blocks a delete)."""
    from app.db.models import Delivery

    count = (
        await session.execute(
            select(func.count()).select_from(Delivery).where(Delivery.channel_id == channel_id)
        )
    ).scalar_one()
    return bool(count)


async def category_channel_matrix(session) -> list[dict]:
    """Each category with the channel designated for it (item 6).

    This is the "which news channel corresponds to this category" half of the
    owner's configuration. Since delivery broadcasts to every active channel,
    this pairing is a LABEL and a provenance marker, not a filter — the panel
    says so on the row.

    ``channel_id`` is None when the category has no designated channel, and 0
    when it has one that was since deleted.
    """
    from app.db.models import Delivery, Field

    categories = (
        await session.execute(select(Category).order_by(Category.name))
    ).scalars().all()
    field_counts = dict(
        (
            await session.execute(
                select(Field.category_id, func.count()).group_by(Field.category_id)
            )
        ).all()
    )
    designated = {
        row.category_id: row.channel_id
        for row in (
            await session.execute(select(CategoryChannelRoute).order_by(CategoryChannelRoute.channel_id))
        ).scalars().all()
    }
    return [
        {
            "id": category.id,
            "name": category.name,
            "description": category.description,
            "field_count": field_counts.get(category.id, 0),
            "channel_id": designated.get(category.id),
        }
        for category in categories
    ]


async def set_category_channel(session, *, category_id: int, channel_id: int | None) -> str:
    """Set (or clear) the channel designated for one category (item 6).

    A category gets exactly one designated channel, so setting a new one
    replaces the previous pairing for that category and leaves other
    categories' pairings alone. Passing ``channel_id=None`` clears it.

    Returns ``"set"`` or ``"cleared"``.

    Raises:
        ValueError: ``unknown-category`` / ``unknown-channel`` when the row
            does not exist, so a stale form cannot write a dangling route.
    """
    category = await session.get(Category, category_id)
    if category is None:
        raise ValueError("unknown-category")
    existing = list(
        (
            await session.execute(
                select(CategoryChannelRoute).where(CategoryChannelRoute.category_id == category_id)
            )
        ).scalars().all()
    )
    if channel_id is None:
        for route in existing:
            await session.delete(route)
        if existing:
            await session.commit()
        return "cleared"
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise ValueError("unknown-channel")
    for route in existing:
        if route.channel_id != channel_id:
            await session.delete(route)
    if not any(route.channel_id == channel_id for route in existing):
        session.add(CategoryChannelRoute(category_id=category_id, channel_id=channel_id))
    await session.commit()
    return "set"


