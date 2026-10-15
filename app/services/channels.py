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


async def last_errors(session) -> dict[int, str]:
    """The newest failure message per channel, for the channels page.

    A channel can be switched on, look perfectly healthy in the list, and
    still reject every message (a bot without admin rights on Telegram, a
    revoked token). Without this the operator only finds out from the delivery
    queue. Returns ``{channel_id: message}``; the text is the provider's own,
    which never contains a credential.
    """
    from app.db.models import Delivery

    rows = (
        await session.execute(
            select(Delivery)
            .where(Delivery.status == "failed", Delivery.error_message.isnot(None))
            .order_by(Delivery.id.desc())
        )
    ).scalars().all()
    out: dict[int, str] = {}
    for row in rows:
        out.setdefault(row.channel_id, row.error_message)
    return out


async def channels_with_counts(session) -> list[Channel]:
    """Channels with ``sent_count`` attached, for the cards and the list.

    One grouped query rather than one per channel, so the page does not fan
    out into N queries as channels are added. Named for what it counts; the
    route counts it used to carry went when routing did.
    """
    from app.db.models import Delivery

    channels = await list_channels(session)
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
    """Flip a channel's active switch.

    Switching **ON** also moves ``activated_at`` to now. That is the whole
    point of the column: a channel that has been off does not get the
    accumulated backlog thrown at it when it comes back, exactly like a brand
    new channel. The owner asked for new news only, and re-enabling a dormant
    channel is the same decision as creating a fresh one.

    Switching **OFF** leaves ``activated_at`` alone, so the boundary does not
    drift while the channel is dark.
    """
    row.is_active = not row.is_active
    if row.is_active:
        # func.now(), not datetime.now(): this must be the SAME clock as the
        # column default, or a channel switched on in the same transaction as
        # an article insert would compare against two different notions of
        # "now" and silently drop that article.
        row.activated_at = func.now()
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
    """Per-channel delivery counts, for the row badges and the delete guard.

    One query per table instead of one per channel, so a page with many
    channels does not fan out into N queries. ``failed`` is included because a
    channel that is switched on but rejecting every message is exactly the
    thing the operator needs to see on the page.
    """
    from app.db.models import Delivery

    all_deliveries = dict(
        (
            await session.execute(
                select(Delivery.channel_id, func.count()).group_by(Delivery.channel_id)
            )
        ).all()
    )
    sent = dict(
        (
            await session.execute(
                select(Delivery.channel_id, func.count())
                .where(Delivery.status.in_(("sent", "deleted_from_channel")))
                .group_by(Delivery.channel_id)
            )
        ).all()
    )
    failed = dict(
        (
            await session.execute(
                select(Delivery.channel_id, func.count())
                .where(Delivery.status == "failed")
                .group_by(Delivery.channel_id)
            )
        ).all()
    )
    return {
        cid: {
            "deliveries": all_deliveries.get(cid, 0),
            "sent": sent.get(cid, 0),
            "failed": failed.get(cid, 0),
        }
        for cid in all_deliveries
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


