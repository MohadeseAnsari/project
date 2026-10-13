"""One-off: move credentials that were pasted into a NAME column into the
encrypted columns (migration 019 follow-up).

Before this change the only way to give a model its own provider key was to
paste it into ``models.api_key_env`` — a column meant for a variable NAME.
Those rows therefore hold a secret in the wrong place, in plaintext, and the
resolver could read it by accident. This script:

- copies ``models.api_key_env`` into ``api_key_encrypted`` and clears the
  column, **only** for values that are not the name of a set environment
  variable (i.e. they are real keys);
- does the same for ``channels.bot_token_env``;
- prints counts only — never a key, its prefix or its length.

Run it once after deploying migration 019:

    docker compose exec api python scripts/migrate_inline_credentials.py

Then rotate every key that was exposed this way (they were in plaintext in the
database, in backups and in any export ever taken).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.credentials import encrypt  # noqa: E402
from app.db.connection import async_session  # noqa: E402
from app.db.models import Channel, Model  # noqa: E402


def looks_like_a_key(value: str | None) -> bool:
    """Whether a value in a *_env column is a pasted secret, not a var name.

    Environment variable names are identifiers; provider keys are not. Any
    value that is not a valid identifier is treated as a key to migrate.
    """
    if not value:
        return False
    if os.environ.get(value):
        return False  # a real variable name — leave it alone
    return not value.replace("_", "").isalnum() or "-" in value or len(value) > 64


async def main() -> int:
    moved_models = cleared_models = 0
    moved_channels = 0
    async with async_session() as session:
        for row in (await session.execute(select(Model))).scalars().all():
            candidate = row.api_key_env
            if not looks_like_a_key(candidate):
                continue
            if not row.api_key_encrypted:
                row.api_key_encrypted = encrypt(candidate)
                moved_models += 1
            row.api_key_env = None
            cleared_models += 1

        for channel in (await session.execute(select(Channel))).scalars().all():
            candidate = channel.bot_token_env
            if not looks_like_a_key(candidate):
                continue
            if not channel.bot_token_encrypted:
                channel.bot_token_encrypted = encrypt(candidate)
                moved_channels += 1
            channel.bot_token_env = None

        await session.commit()

    print(f"models migrated: {moved_models} (of {cleared_models} inline values cleared)")
    print(f"channels migrated: {moved_channels}")
    print("Rotate every key that was stored inline — it was plaintext in the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))