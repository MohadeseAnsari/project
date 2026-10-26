"""Shared test helpers for delivery routing.

Routing is strict: a record publishes only to the channels its CATEGORY names.
Most tests build a world of channels and an article and then assert where it
goes, so every one of them now has to say *which* category routes to *which*
channels -- the thing the owner configures in the panel.

Having one helper for that is the point. When each test wrote its own
``CategoryChannelRoute(...)`` line, a test could pass because it happened to
leave a route behind from an earlier one in the same module, and the suite
would not have caught a resolver that ignored the table entirely. This mirrors
what :func:`app.services.channels.set_category_channels` writes, so a test that
uses it is exercising the real mapping shape.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, CategoryChannelRoute, Channel


async def route_category(
    session: AsyncSession, category: Category, channels
) -> list[Channel]:
    """Map ``category`` to exactly ``channels``, replacing any existing routes.

    Returns the channels, so a caller can assert against the same list it
    routed instead of re-querying and hoping it got the same rows.
    """
    rows = list(channels)
    await session.execute(
        CategoryChannelRoute.__table__.delete().where(
            CategoryChannelRoute.category_id == category.id
        )
    )
    for channel in rows:
        session.add(
            CategoryChannelRoute(category_id=category.id, channel_id=channel.id)
        )
    await session.commit()
    return rows


async def route_all_active(session: AsyncSession, category: Category) -> list[Channel]:
    """Map ``category`` to every active channel -- i.e. the old broadcast.

    For tests whose real subject is something else (the human gate, the batch
    window, channel activation) and which just need news to reach somewhere.

    Tests about routing itself must NOT use this. Routing every category to
    every channel is the one setup that cannot tell "routes were honoured"
    from "routes were ignored", which is precisely the distinction those tests
    exist to make.
    """
    active = (
        (
            await session.execute(
                select(Channel).where(Channel.is_active.is_(True)).order_by(Channel.id)
            )
        )
        .scalars()
        .all()
    )
    return await route_category(session, category, active)