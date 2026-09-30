"""Operational CLI for channels and the field -> channel mapping.

Routes are rows, so moving a field to another channel (or splitting fields
across Bale and Telegram) is a data change, not a deployment. This script is
the safe way to make it, and it refuses to act on ambiguous input instead of
guessing.

Usage:
    python scripts/manage_channels.py list
    python scripts/manage_channels.py routes --field-id 12
    python scripts/manage_channels.py add-channel --platform bale --chat-id 123 --username @news
    python scripts/manage_channels.py route --field-id 12 --channel-id 1
    python scripts/manage_channels.py unroute --field-id 12 --channel-id 1

Idempotent: re-running a route/unroute command is a no-op.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Running this file by path puts scripts/ (not the project root) on sys.path,
# so the app package is unimportable without this. Same convention as
# scripts/seed_fields.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from app.db.connection import async_session, engine  # noqa: E402
from app.db.models import Channel, Field, FieldChannelRoute  # noqa: E402

SUPPORTED_PLATFORMS = ("bale", "telegram")


async def cmd_list() -> None:
    async with async_session() as session:
        channels = (await session.execute(select(Channel).order_by(Channel.id))).scalars().all()
        if not channels:
            print("No channels configured.")
            return
        for c in channels:
            route_count = (
                await session.execute(
                    select(FieldChannelRoute).where(FieldChannelRoute.channel_id == c.id)
                )
            ).scalars().all()
            active = "active" if c.is_active else "inactive"
            print(
                f"[{c.id}] {c.platform} chat_id={c.chat_id} "
                f"username={c.username or '-'} title={c.title or '-'} "
                f"({active}, {len(route_count)} field route(s))"
            )


async def cmd_routes(field_id: int | None) -> None:
    async with async_session() as session:
        query = select(FieldChannelRoute).order_by(
            FieldChannelRoute.field_id, FieldChannelRoute.channel_id
        )
        if field_id is not None:
            query = query.where(FieldChannelRoute.field_id == field_id)
        for route in (await session.execute(query)).scalars().all():
            field = await session.get(Field, route.field_id)
            channel = await session.get(Channel, route.channel_id)
            print(
                f"field_id={route.field_id} ({field.name if field else '?'}) "
                f"-> channel_id={route.channel_id} "
                f"({channel.platform}:{channel.chat_id if channel else '?'})"
            )


async def cmd_add_channel(platform: str, chat_id: int, username: str | None, title: str | None) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        print(f"Unsupported platform {platform!r}; choose from {SUPPORTED_PLATFORMS}", file=sys.stderr)
        raise SystemExit(2)
    async with async_session() as session:
        existing = (
            await session.execute(
                select(Channel).where(Channel.platform == platform).where(Channel.chat_id == chat_id)
            )
        ).scalar_one_or_none()
        if existing:
            print(f"Channel already exists: [{existing.id}] {platform}:{chat_id}")
            return
        channel = Channel(platform=platform, chat_id=chat_id, username=username, title=title)
        session.add(channel)
        await session.commit()
        print(f"Created channel [{channel.id}] {platform}:{chat_id}")


async def cmd_route(field_id: int, channel_id: int) -> None:
    async with async_session() as session:
        if await session.get(Field, field_id) is None:
            print(f"No field with id={field_id}", file=sys.stderr)
            raise SystemExit(1)
        if await session.get(Channel, channel_id) is None:
            print(f"No channel with id={channel_id}", file=sys.stderr)
            raise SystemExit(1)
        session.add(FieldChannelRoute(field_id=field_id, channel_id=channel_id))
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            print(f"Route field_id={field_id} -> channel_id={channel_id} already exists")
            return
        print(f"Routed field_id={field_id} -> channel_id={channel_id}")


async def cmd_unroute(field_id: int, channel_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            delete(FieldChannelRoute)
            .where(FieldChannelRoute.field_id == field_id)
            .where(FieldChannelRoute.channel_id == channel_id)
        )
        await session.commit()
        print(f"Unrouted field_id={field_id} -> channel_id={channel_id} ({result.rowcount} row(s))")


async def main_async(args: argparse.Namespace) -> None:
    try:
        if args.command == "list":
            await cmd_list()
        elif args.command == "routes":
            await cmd_routes(args.field_id)
        elif args.command == "add-channel":
            await cmd_add_channel(args.platform, args.chat_id, args.username, args.title)
        elif args.command == "route":
            await cmd_route(args.field_id, args.channel_id)
        elif args.command == "unroute":
            await cmd_unroute(args.field_id, args.channel_id)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list configured channels")

    p_routes = sub.add_parser("routes", help="list field -> channel routes")
    p_routes.add_argument("--field-id", type=int, default=None)

    p_add = sub.add_parser("add-channel", help="register a channel")
    p_add.add_argument("--platform", required=True, choices=SUPPORTED_PLATFORMS)
    p_add.add_argument("--chat-id", type=int, required=True)
    p_add.add_argument("--username", default=None)
    p_add.add_argument("--title", default=None)

    p_route = sub.add_parser("route", help="map a field to a channel")
    p_route.add_argument("--field-id", type=int, required=True)
    p_route.add_argument("--channel-id", type=int, required=True)

    p_unroute = sub.add_parser("unroute", help="remove a field -> channel route")
    p_unroute.add_argument("--field-id", type=int, required=True)
    p_unroute.add_argument("--channel-id", type=int, required=True)

    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
