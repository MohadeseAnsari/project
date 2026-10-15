"""Field and category reference data for the panel.

Read-heavy reference tables: enriched listings, usage bars, and category
cards with real field names. Classification itself stays in
``app.fields.classification``; the curated-confirm action lives here once
the ``category_source`` column exists (migration 015).
"""

from sqlalchemy import func, or_, select

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
                # The panel marks the current category with `selected` by
                # comparing this to cat.id. Without it the comparison was always
                # false, so the dropdown opened on the FIRST category rather
                # than the field's actual one -- and saving without touching it
                # would silently reassign the field.
                "category_id": field.category_id,
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


async def field_category_rows(
    session, q: str = "", page: int = 1, page_size: int = 50
) -> tuple[list[dict], int]:
    """Fields with their category, for the "field → category" editor.

    The owner's requirement (item 15): the assignment is stored once in the
    database and edited by hand here, instead of a ruleset being re-applied on
    every submission. ``category_source`` records whether the current value was
    set by hand or came from the automatic rules, so the panel can show it.

    Returns ``(rows, total)`` as plain dicts, SEARCHED and PAGINATED. A
    deployment has hundreds of fields, and rendering every one of them as a
    row made the page unusable — the control an operator needs is as rare as
    the ones they want to change.

    Only ``category_source='manual'`` rows are protected from reclassification;
    the rest are listed so the whole mapping is inspectable.
    """
    base = select(Field, Category.name).outerjoin(Category, Category.id == Field.category_id)
    count_query = select(func.count()).select_from(Field)
    term = q.strip()
    if term:
        pattern = f"%{term}%"
        base = base.where(or_(Field.name.ilike(pattern), Field.normalized_name.ilike(pattern)))
        count_query = count_query.where(
            or_(Field.name.ilike(pattern), Field.normalized_name.ilike(pattern))
        )
    total = (await session.execute(count_query)).scalar_one()
    page = max(1, page)
    rows = (
        await session.execute(
            base.order_by(Category.name.is_(None), Category.name, Field.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return (
        [
            {
                "id": field.id,
                "name": field.name,
                "category_id": field.category_id,
                "category_name": category_name or "—",
                "matched_keyword": field.matched_keyword,
                "category_source": field.category_source,
            }
            for field, category_name in rows
        ],
        total,
    )


async def assign_field_category(session, field: Field, category_id: int) -> Field:
    """Pin a field to a category by hand (item 15).

    Sets ``category_source='manual'`` so it is visibly an operator decision and
    is not overwritten by the next automatic classification pass.

    Raises:
        ValueError: ``unknown-category`` when the category does not exist.
    """
    category = await session.get(Category, category_id)
    if category is None:
        raise ValueError("unknown-category")
    field.category_id = category.id
    field.category_source = "manual"
    await session.commit()
    await session.refresh(field)
    return field


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
                # The id is what the panel's category <select> submits. Without
                # it every <option> renders value="" -- the names looked right
                # and saving a category silently did nothing. Nothing caught it
                # because the tests POSTed category_id directly instead of
                # reading the value out of the rendered form; the handler and
                # the template are connected only by that string.
                "id": category.id,
                "name": category.name,
                "description": category.description,
                "field_names": list(names),
                "field_count": count,
            }
        )
    return cards, total
