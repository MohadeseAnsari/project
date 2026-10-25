"""Category reference data and the classification vocabulary.

A category is both a destination and an ISCED-F 2013 classification bucket.
The LLM writes the per-record matches (`main_category_matches`) during
generation; a reviewer may replace them manually on the record. There is no
field-level classification or routing path.

The only configuration here is the category itself and its channel routes
(managed by `app.services.channels`). Deleting a category is refused while any
record still references it, so historical classification never disappears
silently.
"""

from sqlalchemy import func, select

from app.db.models import Category, Main, MainCategoryMatch


async def list_categories(session) -> list[Category]:
    """All categories in id order (selects, matrices, cards)."""
    return list((await session.execute(select(Category).order_by(Category.id))).scalars().all())


async def create_category(session, name: str, description: str | None = None) -> Category:
    """Create a category, or return the existing one with that name.

    Idempotent by name: the panel's form is one text box, and "you already have
    that one" is a more useful answer than a uniqueness error.
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


async def category_match_counts(session, category_ids) -> dict[int, int]:
    """Distinct records with at least one stored match in each category."""
    ids = [int(c) for c in category_ids]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                MainCategoryMatch.category_id,
                func.count(func.distinct(MainCategoryMatch.main_id)),
            )
            .where(MainCategoryMatch.category_id.in_(ids))
            .group_by(MainCategoryMatch.category_id)
        )
    ).all()
    return {category_id: count for category_id, count in rows}


async def category_record_counts(session, category_ids) -> dict[int, int]:
    """Distinct records whose display primary category is in ``category_ids``.

    Used by the delete precheck. Classification history (the matches table) is
    also checked in :func:`delete_category`.
    """
    ids = [int(c) for c in category_ids]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Main.category_id, func.count(Main.id))
            .where(Main.category_id.in_(ids))
            .group_by(Main.category_id)
        )
    ).all()
    return {category_id: count for category_id, count in rows}


async def delete_category(session, category_id: int) -> str:
    """Delete a category that no record references.

    The category is a classification bucket, so removing it while matches or
    primary-category references exist would erase part of a record's history.
    A refused delete keeps the row and its routes intact.

    Raises:
        ValueError: ``not-found`` or ``category-in-use``.
    """
    category = await session.get(Category, category_id)
    if category is None:
        raise ValueError("not-found")
    matches = (
        await session.execute(
            select(func.count()).select_from(MainCategoryMatch)
            .where(MainCategoryMatch.category_id == category_id)
        )
    ).scalar_one()
    primary = (
        await session.execute(
            select(func.count()).select_from(Main)
            .where(Main.category_id == category_id)
        )
    ).scalar_one()
    if matches or primary:
        raise ValueError("category-in-use")
    await session.delete(category)
    await session.commit()
    return "deleted"


async def category_cards(session, q: str = "") -> tuple[list[dict], int]:
    """Category cards with their record usage, plus the total category count."""
    query = select(Category).order_by(Category.id)
    if q.strip():
        query = query.where(Category.name.ilike(f"%{q.strip()}%"))
    rows = list((await session.execute(query)).scalars().all())
    total = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
    ids = [row.id for row in rows]
    counts = await category_match_counts(session, ids)
    primary = await category_record_counts(session, ids)
    cards = [
        {
            "id": category.id,
            "name": category.name,
            "description": category.description,
            "deprecated": category.deprecated,
            "record_count": counts.get(category.id, 0),
            "primary_count": primary.get(category.id, 0),
        }
        for category in rows
    ]
    return cards, total
