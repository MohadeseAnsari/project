"""Field and category reference data for the panel.

Read-heavy reference tables: enriched listings, usage bars, and category
cards with real field names. Classification itself stays in
``app.fields.classification``; the curated-confirm action lives here once
the ``category_source`` column exists (migration 015).
"""

from sqlalchemy import func, select

from app.db.models import Category, Channel, Field, FieldChannelRoute, Main


async def list_categories(session) -> list[Category]:
    """All categories in id order (selects, matrices, cards)."""
    return list((await session.execute(select(Category).order_by(Category.id))).scalars().all())


async def list_field_options(session) -> list[dict]:
    """Compact ``{id, name}`` options for routing-picker selects."""
    return [
        {"id": fid, "name": fname}
        for fid, fname in (
            await session.execute(select(Field.id, Field.name).order_by(Field.name))
        ).all()
    ]


async def list_fields(session, limit: int = 100) -> list[dict]:
    """Newest fields with category, matched keyword, and routed channels."""
    rows = (await session.execute(select(Field).order_by(Field.id.desc()).limit(limit))).scalars().all()
    fields = []
    for field in rows:
        category = await session.get(Category, field.category_id) if field.category_id else None
        routed = (
            await session.execute(
                select(Channel)
                .join(FieldChannelRoute, FieldChannelRoute.channel_id == Channel.id)
                .where(FieldChannelRoute.field_id == field.id)
            )
        ).scalars().all()
        fields.append(
            {
                "id": field.id,
                "name": field.name,
                "source": field.source,
                "category_name": category.name if category else None,
                "matched_keyword": field.matched_keyword,
                "category_source": field.category_source,
                "channel_names": "، ".join(c.username or c.title or c.platform for c in routed) or None,
            }
        )
    return fields


async def field_total(session) -> int:
    """Total field rows (table badge)."""
    return (await session.execute(select(func.count()).select_from(Field))).scalar_one()


async def get_field(session, field_id: int) -> Field | None:
    """Fetch one field row by id (panel routes never touch the ORM directly)."""
    return await session.get(Field, field_id)


async def confirm_category(session, field: Field) -> Field:
    """Freeze a field's category as human-curated (reclassification skips it).

    The reviewer has read the matched keyword and agrees: future rule
    updates and ``--apply`` runs must never move this row again.
    """
    field.category_source = "curated"
    await session.commit()
    await session.refresh(field)
    return field


async def top_fields(session, limit: int = 3) -> list[dict]:
    """Most-used thesis fields with percentage bars (real record counts)."""
    top_rows = (
        await session.execute(
            select(Main.field, func.count()).group_by(Main.field).order_by(func.count().desc()).limit(limit)
        )
    ).all()
    top_total = sum(n for _, n in top_rows) or 1
    return [{"name": name, "pct": int(round(n / top_total * 100))} for name, n in top_rows]


async def category_cards(session, q: str = "") -> tuple[list[dict], int]:
    """Category cards with real field-name chips, plus the total count."""
    query = select(Category).order_by(Category.id)
    if q.strip():
        query = query.where(Category.name.ilike(f"%{q.strip()}%"))
    rows = (await session.execute(query)).scalars().all()
    total = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
    cards = []
    for category in rows:
        names = (
            await session.execute(
                select(Field.name).where(Field.category_id == category.id).order_by(Field.id).limit(9)
            )
        ).scalars().all()
        count = (
            await session.execute(
                select(func.count()).select_from(Field).where(Field.category_id == category.id)
            )
        ).scalar_one()
        cards.append(
            {
                "name": category.name,
                "description": category.description,
                "field_names": list(names),
                "field_count": count,
            }
        )
    return cards, total
