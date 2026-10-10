"""Re-classify ``source='spreadsheet'`` fields with the current classifier.

Dry-run by default: prints a diff table (field, old category, new category,
matched keyword) for owner review and writes nothing. Pass ``--apply`` to
persist the new categories and matched keywords.

``main`` rows are NEVER touched: each record keeps the ``category_id``
snapshotted at processing time, so history stays stable while fields move.
The panel shows both values on the record page (stored vs current).

Usage:
    python scripts/reclassify_fields.py            # dry-run diff to stdout
    python scripts/reclassify_fields.py --apply    # write new categories
"""

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.fields.classification import classify_with_reason
from app.fields.normalization import normalize_field


async def collect_diff(conn) -> list[tuple]:
    """Compare every spreadsheet field's stored category with a fresh classify.

    Curated rows (``category_source='curated'``) are skipped permanently:
    human decisions survive rule updates. Returns
    ``(name, old_category, new_category, matched_keyword)`` rows for fields
    that would change (plus their current keyword for context).
    """
    rows = await conn.fetch(
        """
        SELECT f.id, f.name, f.normalized_name, f.matched_keyword, f.category_source, c.name AS category
        FROM fields f
        JOIN categories c ON c.id = f.category_id
        WHERE f.source = 'spreadsheet' AND f.category_source != 'curated'
        ORDER BY f.id
        """
    )
    diff = []
    for row in rows:
        category, keyword, _ = classify_with_reason(row["normalized_name"] or normalize_field(row["name"]))
        if category != row["category"]:
            diff.append((row["name"], row["category"], category, keyword))
    return diff


async def main() -> None:
    parser = argparse.ArgumentParser(description="Diff (default) or apply field re-classification.")
    parser.add_argument("--apply", action="store_true", help="Write new categories and keywords.")
    args = parser.parse_args()

    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    try:
        diff = await collect_diff(conn)
        print(f"{'field':<50} {'old':<28} {'new':<28} keyword")
        for name, old, new, keyword in diff:
            print(f"{name:<50} {old:<28} {new:<28} {keyword or '—'}")
        print(f"\n{len(diff)} field(s) would change.")
        if not args.apply:
            print("Dry run: nothing written. Re-run with --apply to persist.")
            return
        changed = 0
        for name, _old, new, keyword in diff:
            cat_id = await conn.fetchval("SELECT id FROM categories WHERE name = $1", new)
            if cat_id is None:
                print(f"SKIP {name}: unknown category {new}")
                continue
            await conn.execute(
                "UPDATE fields SET category_id = $1, matched_keyword = $2 WHERE name = $3",
                cat_id, keyword, name,
            )
            changed += 1
        print(f"Applied: {changed} field(s) updated. `main` rows untouched (snapshot kept).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
