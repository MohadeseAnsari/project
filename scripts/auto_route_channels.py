"""Auto-create category_channel_routes based on channel titles.

For each active, non-deprecated channel:
  1. Try to find a non-deprecated category whose normalized name matches
     the channel's normalized title (exact match, after NFC + trim + collapse).
  2. If no match on title, try matching on username (strip leading '@').
  3. If a match is found AND no route already exists -> insert into
     category_channel_routes.
  4. If no match is found -> log a warning naming the channel, so the
     operator can fix the title or manually route it in the panel.

The "other" category is never auto-routed (it is a fallback, not a real
destination).

Idempotent: ON CONFLICT DO NOTHING on the (category_id, channel_id) pair.

Usage:
    python scripts/auto_route_channels.py            # dry-run, print plan
    python scripts/auto_route_channels.py --apply    # actually write

Exit code 0 on success, 1 if any channel had NO MATCH.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from app.config import settings
from app.fields.normalization import normalize_field


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return normalize_field(s.lstrip("@")) if False else normalize_field(s)


async def main(apply: bool) -> int:
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    try:
        channels = await conn.fetch(
            "SELECT id, title, username FROM channels WHERE is_active = TRUE ORDER BY id"
        )
        cats = await conn.fetch(
            "SELECT id, name FROM categories WHERE deprecated = FALSE"
        )
        # normalized name -> category id (skip "other")
        cat_by_norm: dict[str, tuple[int, str]] = {}
        for c in cats:
            if c["name"] == "other":
                continue
            cat_by_norm[normalize_field(c["name"])] = (c["id"], c["name"])

        no_match = False
        matched = 0
        already = 0
        for ch in channels:
            cid = ch["id"]
            title = ch["title"] or ""
            username = (ch["username"] or "").lstrip("@")
            title_norm = normalize_field(title)
            user_norm = normalize_field(username)

            target = cat_by_norm.get(title_norm) or cat_by_norm.get(user_norm)
            if target is None:
                no_match = True
                print(
                    f'NO MATCH: channel "{title}" (title="{title}", username="{ch["username"]}") '
                    f"— no category name matches; fix title or route manually in panel"
                )
                continue

            cat_id, cat_name = target
            exists = await conn.fetchval(
                "SELECT 1 FROM category_channel_routes WHERE category_id=$1 AND channel_id=$2",
                cat_id, cid,
            )
            if exists:
                already += 1
                print(f'ALREADY ROUTED: channel "{title}" -> category "{cat_name}" (route exists)')
                continue

            matched += 1
            print(f'MATCHED: channel "{title}" -> category "{cat_name}" (id={cat_id})')
            if apply:
                await conn.execute(
                    "INSERT INTO category_channel_routes (category_id, channel_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    cat_id, cid,
                )

        print()
        print(f"Summary: matched={matched}, already_routed={already}, no_match={no_match}")
        if apply:
            print("Applied.")
        else:
            print("Dry-run: re-run with --apply to write.")
        return 1 if no_match else 0
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-route channels to categories by title.")
    parser.add_argument("--apply", action="store_true", help="Write category_channel_routes rows.")
    args = parser.parse_args()
    code = asyncio.run(main(args.apply))
    sys.exit(code)