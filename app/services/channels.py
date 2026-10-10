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
    """Channels with ``route_count`` attached for the overview cards."""
    channels = await list_channels(session)
    for channel in channels:
        channel.route_count = (
            await session.execute(
                select(func.count()).select_from(FieldChannelRoute).where(
                    FieldChannelRoute.channel_id == channel.id
                )
            )
        ).scalar_one()
    return channels


async def resolve_target(platform: str, target: str) -> dict:
    """Validate a numeric id or @username live via getChat before saving.

    Returns the provider result (numeric ``id``, ``username``, ``title``).

    Raises:
        ValueError: ``bad-platform`` for unknown platforms,
            ``no-platform-token`` when creation has no token to validate
            with, ``unresolvable: ...`` when getChat rejects the target,
            ``bad-result`` when the provider answer has no numeric id.
    """
    from app.messaging.platforms import DeliveryError, configured_clients

    if platform not in ("bale", "telegram"):
        raise ValueError("bad-platform")
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


async def create_channel(
    session,
    *,
    platform: str,
    target: str,
    title: str | None = None,
    bot_token_env: str | None = None,
) -> Channel:
    """Validate the target live, then insert the destination row.

    Raises:
        ValueError: ``duplicate-channel`` when the (platform, chat_id) pair
            exists, plus the ``resolve_target`` failures above.
    """
    resolved = await resolve_target(platform, target)
    row = Channel(
        platform=platform,
        chat_id=resolved["chat_id"],
        username=resolved["username"],
        title=title.strip() or resolved["title"] if title else resolved["title"],
        bot_token_env=(bot_token_env or "").strip() or None,
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
    """Update display fields (and the token variable name); tokens never land here."""
    allowed = {"title", "username", "chat_id", "bot_token_env"}
    for field_name, value in updates.items():
        if field_name in allowed:
            setattr(row, field_name, value)
    await session.commit()
    await session.refresh(row)
    return row


async def toggle_channel(session, row: Channel) -> Channel:
    """Flip a channel's active switch."""
    row.is_active = not row.is_active
    await session.commit()
    await session.refresh(row)
    return row


async def add_field_route(session, *, field_id: int, channel_id: int) -> bool:
    """Insert a field exception route; False when it already exists."""
    session.add(FieldChannelRoute(field_id=field_id, channel_id=channel_id))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        return False
    return True


async def remove_field_route(session, *, field_id: int, channel_id: int) -> bool:
    """Remove a field exception route; False when there was none."""
    row = await session.get(FieldChannelRoute, (field_id, channel_id))
    if not row:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def add_category_route(session, *, category_id: int, channel_id: int) -> bool:
    """Insert a category route (the primary mapping); False if it exists."""
    session.add(CategoryChannelRoute(category_id=category_id, channel_id=channel_id))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        return False
    return True


async def remove_category_route(session, *, category_id: int, channel_id: int) -> bool:
    """Remove a category route; False when there was none."""
    row = await session.get(CategoryChannelRoute, (category_id, channel_id))
    if not row:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def field_route_matrix(session, limit: int = 30) -> list[dict]:
    """Field→channel tick matrix rows (``field_name``, ``channel_ids``)."""
    fields = (await session.execute(select(Field).order_by(Field.id).limit(limit))).scalars().all()
    matrix = []
    for field in fields:
        ids = set(
            (
                await session.execute(
                    select(FieldChannelRoute.channel_id).where(FieldChannelRoute.field_id == field.id)
                )
            ).scalars().all()
        )
        matrix.append({"field_name": field.name, "channel_ids": ids})
    return matrix


async def category_route_matrix(session) -> tuple[list[Category], list[dict]]:
    """All categories plus their routed channel ids for the primary matrix."""
    categories = (await session.execute(select(Category).order_by(Category.id))).scalars().all()
    routes = []
    for category in categories:
        ids = set(
            (
                await session.execute(
                    select(CategoryChannelRoute.channel_id).where(
                        CategoryChannelRoute.category_id == category.id
                    )
                )
            ).scalars().all()
        )
        routes.append({"category_id": category.id, "category_name": category.name, "channel_ids": ids})
    return categories, routes
