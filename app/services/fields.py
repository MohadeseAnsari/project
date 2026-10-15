"""Field and category reference data for the panel.

Read-heavy reference tables: enriched listings, usage bars, and category
cards with real field names. Classification itself stays in
``app.fields.classification``; the curated-confirm action lives here once
the ``category_source`` column exists (migration 015).
"""

from sqlalchemy import and_, func, or_, select

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
    session, q: str = "", page: int = 1, page_size: int = 50,
    category_id: int | None = None, source: str = "", unconfirmed_only: bool = False,
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

    Every filter is applied INSIDE both queries. That distinction is the whole
    point: filtering after the LIMIT means the page you are shown is not the
    page the count claims, and "next" walks off the end of the real result set.
    ``total`` and the rows must come from the same predicate, or the pager lies.

    This is the function the panel SHOULD have been using all along. The fields
    page called ``list_fields()``, which has a default ``limit=100`` and was
    called with no argument -- so 449 of 549 fields were never rendered, and
    because the search box then filtered that already-truncated list in Python,
    those 449 could not be found by searching either.
    """
    base = select(Field, Category.name).outerjoin(Category, Category.id == Field.category_id)
    count_query = select(func.count()).select_from(Field)
    filters = []
    term = q.strip()
    if term:
        pattern = f"%{term}%"
        filters.append(
            or_(Field.name.ilike(pattern), Field.normalized_name.ilike(pattern))
        )
    if category_id:
        filters.append(Field.category_id == category_id)
    if source:
        filters.append(Field.category_source == source)
    if unconfirmed_only:
        # "Not yet confirmed by a human" is what an operator works through, so
        # it is a first-class filter rather than something to spot by eye.
        filters.append(Field.category_source.notin_(("manual", "curated")))
    if filters:
        where = and_(*filters)
        base = base.where(where)
        count_query = count_query.where(where)

    total = (await session.execute(count_query)).scalar_one()
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
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
                # The table shows the ingestion source under the name.
                "source": field.source,
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
