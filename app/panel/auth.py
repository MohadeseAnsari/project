"""Panel authentication: argon2 passwords plus signed cookies.

No new secrets are introduced: the admin password and the cookie signing key
come from environment variables only (``PANEL_ADMIN_PASSWORD`` /
``PANEL_SECRET_KEY``). Passwords are stored as argon2 hashes, never plaintext.
"""

import logging

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings
from app.services.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

_cookie_name = "panel_session"

#: Loopback peers are always the trusted proxy path.
_LOOPBACK_PEERS = ("127.0.0.1", "::1")


def _trusted_proxy_nets():
    """Parse PANEL_TRUSTED_PROXIES into ipaddress networks (cached per call).

    Plain IPs become /32 (/128) nets so both forms work. A malformed entry
    is ignored rather than failing open.
    """
    import ipaddress

    nets = []
    for entry in (settings.panel_trusted_proxies or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def _peer_is_trusted_proxy(peer: str) -> bool:
    """Whether this direct peer may supply X-Forwarded-* headers."""
    if peer in _LOOPBACK_PEERS:
        return True
    import ipaddress

    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_proxy_nets())


def _session_max_age() -> int:
    """Session lifetime in seconds (cookie and server-side check agree)."""
    return int(settings.panel_session_hours * 3600)


def client_ip(request: Request) -> str:
    """Real visitor IP behind the trusted reverse proxy.

    Only a trusted proxy peer (loopback, or a network in
    ``PANEL_TRUSTED_PROXIES`` such as the compose bridge subnet the proxy
    arrives on) may supply ``X-Forwarded-For``; anyone else's header is
    ignored, so a visitor can never spoof the address the login lockout and
    audit trail record.
    """
    peer = request.client.host if request.client else ""
    if peer and _peer_is_trusted_proxy(peer):
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if first:
            return first
    return peer or "unknown"


def forwarded_proto(request: Request) -> str:
    """Edge protocol from the trusted proxy (empty for direct clients)."""
    peer = request.client.host if request.client else ""
    if peer and _peer_is_trusted_proxy(peer):
        return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return ""


def _serializer() -> URLSafeTimedSerializer:
    """Cookie signer bound to the configured panel secret."""
    return URLSafeTimedSerializer(settings.panel_secret_key, salt="panel-session")


def set_session_cookie(response, user_id: int, *, secure: bool) -> None:
    """Sign the user id into an HttpOnly session cookie (no JS access).

    The caller passes ``secure=True`` when ``PANEL_COOKIE_SECURE`` is on or
    the trusted proxy reports ``https``; over plain HTTP browsers would drop
    a Secure cookie and nobody could log in.
    """
    token = _serializer().dumps({"uid": user_id})
    response.set_cookie(
        _cookie_name,
        token,
        max_age=_session_max_age(),
        httponly=True,
        samesite="lax",
        secure=secure,
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
        data = _serializer().loads(token, max_age=_session_max_age())
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


async def ensure_admin_user() -> None:
    """Create the first admin from env vars when no users exist yet.

    Thin delegation to the users service: the panel layer performs no
    database access of its own (enforced by ``tests/test_panel_layering.py``).
    """
    from app.services import users as user_service

    await user_service.ensure_initial_admin(
        settings.panel_admin_username, settings.panel_admin_password
    )
