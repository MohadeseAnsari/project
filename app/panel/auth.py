"""Panel authentication: argon2 passwords plus signed cookies.

No new secrets are introduced: the admin password and the cookie signing key
come from environment variables only (``PANEL_ADMIN_PASSWORD`` /
``PANEL_SECRET_KEY``). Passwords are stored as argon2 hashes, never plaintext.
"""

import logging

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select

from app.config import settings
from app.db.models import PanelUser, Role

logger = logging.getLogger(__name__)

_ph = PasswordHasher()
_cookie_name = "panel_session"
_cookie_max_age = 12 * 3600


def _serializer() -> URLSafeTimedSerializer:
    """Cookie signer bound to the configured panel secret."""
    return URLSafeTimedSerializer(settings.panel_secret_key, salt="panel-session")


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage (argon2, never reversible)."""
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Check a password against its stored hash (constant-time via argon2)."""
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def set_session_cookie(response, user_id: int) -> None:
    """Sign the user id into an HttpOnly cookie (12h, Lax, no JS access)."""
    token = _serializer().dumps({"uid": user_id})
    response.set_cookie(
        _cookie_name,
        token,
        max_age=_cookie_max_age,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response) -> None:
    """Remove the session cookie on logout."""
    response.delete_cookie(_cookie_name)


def session_user_id(request: Request) -> int | None:
    """Return the signed user id, or None for missing/invalid cookies."""
    token = request.cookies.get(_cookie_name)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=_cookie_max_age)
        uid = data.get("uid")
        return int(uid) if uid is not None else None
    except (BadSignature, ValueError, TypeError):
        return None


async def audit(session, user_id: int | None, action: str, main_id: int | None = None, detail: str | None = None) -> None:
    """Record who did what (review/retry/config) for the record history page."""
    from app.db.models import AuditLog

    session.add(AuditLog(user_id=user_id, action=action, main_id=main_id, detail=detail))
    await session.commit()


async def ensure_admin_user() -> None:
    """Create the first admin from env vars when no users exist yet.

    Runs at panel startup only. The password comes from ``PANEL_ADMIN_PASSWORD``
    and is hashed before storage; when the variable is empty nothing is created
    and login stays disabled until it is configured.
    """
    from app.db.connection import async_session

    if not settings.panel_admin_password:
        logger.warning("PANEL_ADMIN_PASSWORD is empty; panel login is disabled")
        return
    async with async_session() as session:
        existing = (await session.execute(select(PanelUser).limit(1))).scalars().first()
        if existing:
            return
        role = (
            await session.execute(select(Role).where(Role.name == "admin"))
        ).scalar_one_or_none()
        if not role:
            role = Role(name="admin", description="Full access")
            session.add(role)
            await session.flush()
        session.add(
            PanelUser(
                username=settings.panel_admin_username,
                password_hash=hash_password(settings.panel_admin_password),
                role_id=role.id,
                is_active=True,
            )
        )
        await session.commit()
        logger.info("Created initial panel admin username=%s", settings.panel_admin_username)
