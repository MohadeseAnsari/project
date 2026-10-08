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
    """Sign the user id into an HttpOnly cookie (12h, Lax, no JS access).

    ``Secure`` is sent only when ``PANEL_COOKIE_SECURE`` is on (TLS reverse
    proxy in front); over plain HTTP browsers would drop a Secure cookie and
    nobody could log in.
    """
    token = _serializer().dumps({"uid": user_id})
    response.set_cookie(
        _cookie_name,
        token,
        max_age=_cookie_max_age,
        httponly=True,
        samesite="lax",
        secure=settings.panel_cookie_secure,
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


# --------------------------------------------------------------------------
# login guardrails (per-IP rate limiting + lockout for public exposure)
# --------------------------------------------------------------------------

# Process-local failure log: ip -> sorted list of failure timestamps, plus
# ip -> lockout expiry. The web service runs as a single replica, so memory
# is sufficient; restarts clear counters while the audit trail keeps history.
_login_failures: dict[str, list] = {}
_login_locked_until: dict[str, object] = {}


def _login_now():
    from datetime import datetime

    return datetime.utcnow()


def is_login_locked(ip: str) -> bool:
    """Whether this IP is currently locked out after repeated failures."""
    from datetime import timedelta

    expiry = _login_locked_until.get(ip)
    if expiry is None:
        return False
    if _login_now() >= expiry:
        _login_locked_until.pop(ip, None)
        return False
    return True


def record_login_failure(ip: str) -> bool:
    """Record a failed login; return True when this failure triggers lockout.

    Sliding window: only failures inside ``panel_login_window_minutes``
    count toward ``panel_login_max_attempts``; reaching the cap locks the IP
    for ``panel_login_lockout_minutes``.
    """
    from datetime import timedelta

    now = _login_now()
    window_start = now - timedelta(minutes=settings.panel_login_window_minutes)
    recent = [t for t in _login_failures.get(ip, []) if t >= window_start]
    recent.append(now)
    _login_failures[ip] = recent
    if len(recent) >= settings.panel_login_max_attempts:
        _login_locked_until[ip] = now + timedelta(minutes=settings.panel_login_lockout_minutes)
        return True
    return False


def clear_login_failures(ip: str) -> None:
    """Forget failures/lockout for an IP (successful login, or tests)."""
    _login_failures.pop(ip, None)
    _login_locked_until.pop(ip, None)


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
