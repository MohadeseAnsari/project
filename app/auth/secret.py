"""Shared-secret authentication.

The secret comes from environment configuration only. It is never stored in
the database, never logged, and never sent to the LLM.
"""

import hmac

from app.config import settings


def verify_secret(provided: str) -> bool:
    """Constant-time comparison of the provided secret against the configured one."""
    return hmac.compare_digest(provided, settings.shared_secret)
