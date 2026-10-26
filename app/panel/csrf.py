"""CSRF protection for panel forms: session-bound tokens + origin checking.

Model: the token is an HMAC of the signed session cookie value keyed by
``settings.panel_secret_key`` — bound to the session, stateless (no extra
server-side store), and different per login because the session token
changes. Every panel POST form embeds it via ``csrf_field(request)``.

Enforcement runs as a FastAPI dependency (``verify_csrf``), NOT as HTTP
middleware: dependencies share the same ``Request`` object as the endpoint,
so ``await request.form()`` caches and the endpoint's ``Form(...)`` params
still parse. Reading the form in ``@app.middleware("http")`` consumes the
ASGI receive stream on a different Request object and downstream forms
arrive empty (422). ``/login`` is pre-session, so its CSRF defense is the
Origin check (the documented exemption — a session-bound token cannot exist
before the session does). ``/logout`` is exempt too (signed-cookie deletion
only).
"""

import hashlib
import hmac
import logging

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.config import settings

logger = logging.getLogger(__name__)

#: Anonymous derivation input: the login form has no session token yet, so
#: its field token derives from this fixed string instead (Origin check carries
#: the actual login-CSRF defense).
_ANON_INPUT = "panel-anonymous"

_EXEMPT_PATHS = {"/login", "/logout"}


def _csrf_token(request: Request) -> str:
    """Derive the CSRF token for this request's session (or the anonymous one)."""
    return csrf_token_for_cookie(request.cookies.get("panel_session", ""))


def csrf_token_for_cookie(cookie_value: str) -> str:
    """Derive the CSRF token for a raw session-cookie value (tests + middleware).

    Single HMAC derivation shared by ``_csrf_token`` (request path) and test
    helpers that only have the cookie jar, so tests compute exactly the token
    the server expects without parsing HTML.
    """
    token = cookie_value or _ANON_INPUT
    return hmac.new(
        settings.panel_secret_key.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def csrf_field(request: Request) -> str:
    """Render the hidden input every panel POST form must carry.

    Jinja global — templates call ``{{ csrf_field(request) }}`` inside each
    ``<form method="post">`` so the token rides along automatically.
    """
    value = _csrf_token(request)
    return f'<input type="hidden" name="csrf_token" value="{value}">'


def _origin_allowed(request: Request) -> bool:
    """Same-origin rule: an Origin header must match the request's own host.

    Absent Origin (plain form posts from this site) is allowed; a present but
    foreign Origin never is. This covers cross-site form posts the token
    alone cannot (e.g. a forged login form).
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    try:
        from urllib.parse import urlsplit

        origin_host = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(host) and hmac.compare_digest(origin_host, host)


async def validate_panel_post(request: Request) -> HTMLResponse | None:
    """Validate CSRF for one panel POST; return a Persian 400 page or None.

    Legacy entry point kept for direct callers; the panel itself enforces
    via the ``verify_csrf`` dependency (same checks, exception form).
    Reading the form here is safe only when sharing the endpoint's Request
    object (dependency path) — see the module docstring for why middleware
    must not call this.
    """
    try:
        await verify_csrf(request)
    except CSRFError as e:
        return HTMLResponse(
            f'<meta charset="utf-8"><div class="note">{e.message}</div>',
            status_code=400,
        )
    return None


class CSRFError(Exception):
    """Failed CSRF check with a Persian message for the 400 page."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency: enforce CSRF on panel POSTs, raise CSRFError on failure.

    Checks: Origin/Host match (always, including exempt paths), then the
    session-bound token for non-exempt paths. Safe as a dependency because
    it shares the endpoint's Request object, so ``await request.form()``
    caches for the endpoint's own ``Form(...)`` parsing.
    """
    if request.method != "POST":
        return
    if not _origin_allowed(request):
        logger.warning("CSRF origin check failed path=%s", request.url.path)
        raise CSRFError("درخواست از مبدأ ناشناخته رد شد.")
    if request.url.path in _EXEMPT_PATHS:
        return
    form = await request.form()
    supplied = form.get("csrf_token") or request.headers.get("x-csrf-token") or ""
    expected = _csrf_token(request)
    if not supplied or not hmac.compare_digest(str(supplied), expected):
        logger.warning("CSRF token check failed path=%s", request.url.path)
        raise CSRFError("توکن امنیتی نامعتبر است؛ صفحه را دوباره باز کنید.")
