"""Apply SQL migrations from ``migrations/`` in filename order.

Tracks applied files in the ``_migrations`` table so each migration runs
exactly once. This is the primary schema mechanism — ``create_all()`` is
never used to create the production schema.
"""

import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings


async def apply_migrations():
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                id SERIAL PRIMARY KEY,
                filename VARCHAR(255) UNIQUE NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """
        )

        migrations_dir = Path(__file__).parent.parent / "migrations"
        files = sorted(migrations_dir.glob("*.sql"))

        for f in files:
            row = await conn.fetchrow(
                "SELECT 1 FROM _migrations WHERE filename = $1", f.name
            )
            if row:
                print(f"SKIP  {f.name}")
                continue
            sql = f.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations (filename) VALUES ($1)", f.name
                )
            print(f"APPLY {f.name}")

        print("Done.")
        try:
            from app.news.isced import ISCED_CATEGORIES, OTHER_CATEGORY

            expected = {name for name, _ in ISCED_CATEGORIES} | {OTHER_CATEGORY}
            rows = await conn.fetch(
                "SELECT name FROM categories WHERE short_code IS NOT NULL"
            )
            actual = {r["name"] for r in rows}
            missing = expected - actual
            if missing:
                print("WARNING: categories missing short_code:", sorted(missing))
            if len(rows) != len(expected):
                print(
                    "WARNING: expected %d short codes, found %d"
                    % (len(expected), len(rows))
                )
        except Exception as exc:  # pragma: no cover - verification only
            print("WARNING: could not verify short codes:", exc)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(apply_migrations())
