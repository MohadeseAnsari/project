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


async def create_category(session, name: str, description: str | None = None) -> Category:
    """Create a category, or return the existing one with that name.

    Idempotent by name rather than raising on a duplicate, because the panel's
    form is a single text box and "you already have that one" is a far more
    useful answer than a uniqueness error the operator cannot act on. Routing
    is decided per category, so being unable to create one means a bucket of
    fields that cannot be routed anywhere.

    The name is trimmed but NOT otherwise normalized: two categories differing
    only in internal whitespace would be a confusing near-duplicate, and the
    collapse is cheap enough to be safe.
    """
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValueError("empty-name")
    existing = (
        await session.execute(select(Category).where(Category.name == trimmed))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    category = Category(name=trimmed, description=(description or "").strip() or None)
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category


async def create_field(session, name: str, category_id: int) -> Field:
    """Add a field by name and place it in a category.

    ``normalized_name`` is the stable identity used for exact matching and
    duplicate prevention, so it is computed the same way the classifier
    computes it. Skipping that would let a field be added under a name that
    looks new but normalizes onto an existing one, and the second insert would
    blow up on the unique index with an error the operator cannot read.

    ``source='manual'`` marks it as an operator addition rather than something
    the spreadsheet supplied, so the field list can tell the two apart and a
    later spreadsheet import does not mistake it for its own.

    Raises:
        ValueError: ``empty-name`` / ``duplicate-field`` / ``unknown-category``
    """
    from app.fields.normalization import normalize_field

    trimmed = (name or "").strip()
    if not trimmed:
        raise ValueError("empty-name")
    normalized = normalize_field(trimmed)
    if not normalized:
        raise ValueError("empty-name")
    if await session.get(Category, category_id) is None:
        raise ValueError("unknown-category")
    clash = (
        await session.execute(
            select(Field).where(
                or_(Field.name == trimmed, Field.normalized_name == normalized)
            )
        )
    ).scalars().first()
    if clash is not None:
        raise ValueError("duplicate-field")
    field = Field(
        name=trimmed,
        normalized_name=normalized,
        category_id=category_id,
        source="manual",
        # A hand-added field is already a decision, so it must not be moved by
        # the next automatic classification pass the way a ruleset guess would.
        category_source="manual",
    )
    session.add(field)
    await session.commit()
    await session.refresh(field)
    return field


async def bulk_assign_category(
    session, field_ids, category_id: int
) -> tuple[int, int]:
    """Move every field in ``field_ids`` into one category. Returns (moved, skipped).

    This is the operation that makes curation possible at the scale the data is
    actually at: a deployment has hundreds of fields and the classifier drops
    most of them into one catch-all, so fixing them one dropdown at a time is
    not a task anyone finishes.

    Fields already in the target are counted as ``skipped`` rather than
    rewritten. Rewriting them would set ``category_source='manual'`` on rows
    the operator never looked at, quietly converting a machine guess into an
    apparent human decision -- and the field list shows that badge, so the
    difference is meant to mean something.

    Unknown ids are ignored rather than fatal: the form posts whatever was
    ticked, and a stale checkbox must not block the rest of the batch.

    Raises:
        ValueError: ``unknown-category`` when the target does not exist.
    """
    if await session.get(Category, category_id) is None:
        raise ValueError("unknown-category")
    ids = []
    for raw in field_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    if not ids:
        return (0, 0)
    rows = list(
        (
            await session.execute(select(Field).where(Field.id.in_(ids)))
        )
        .scalars()
        .all()
    )
    moved = 0
    skipped = 0
    for row in rows:
        if row.category_id == category_id:
            skipped += 1
            continue
        row.category_id = category_id
        row.category_source = "manual"
        moved += 1
    if moved:
        await session.commit()
    return (moved, skipped)


async def field_record_count(session, field_id: int) -> int:
    """How many records point at this field.

    ``main.field_id`` is a plain FK, so deleting a referenced field would raise
    a database error the operator cannot read. The panel counts first and
    refuses in Persian instead.
    """
    return (
        await session.execute(
            select(func.count(Main.id)).where(Main.field_id == field_id)
        )
    ).scalar_one()


async def delete_field(session, row: Field) -> str:
    """Delete a field, refusing while any record still points at it.

    Refuses rather than detaching. ``main.field_id`` is nullable, so setting
    it to NULL would be easy -- but that silently detaches real history from
    the discipline it was filed under, and the record's own ``field`` text
    still says otherwise. Losing that link quietly is worse than being told
    the field cannot go yet.

    Raises:
        ValueError: ``field-in-use`` when records still reference it.
    """
    if await field_record_count(session, row.id):
        raise ValueError("field-in-use")
    await session.delete(row)
    await session.commit()
    return "deleted"


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


async def category_field_counts(session, category_ids) -> dict[int, int]:
    """How many fields sit in each of ``category_ids``.

    Used to say how much work a category with no channel route is stranding.
    Counting is in the unit the owner thinks in -- a field is a discipline that
    can generate news -- rather than "categories with no routes", which is
    always the same list and therefore never actionable.

    One grouped query for the whole set: the panel asks about every category on
    every render, and a deployment has hundreds, so a per-category lookup would
    be hundreds of round trips to produce a number that fits on one line.
    Categories with no fields are simply absent from the result.
    """
    ids = [int(c) for c in category_ids]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Field.category_id, func.count(Field.id))
            .where(Field.category_id.in_(ids))
            .group_by(Field.category_id)
        )
    ).all()
    return {category_id: count for category_id, count in rows}


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
