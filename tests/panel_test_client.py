"""CSRF-aware test client for panel tests (compliant-browser simulation).

Existing panel tests exercise business logic (review, users, channels); they
run as a compliant browser that already carries the session-bound CSRF token
rendered into every POST form (see ``app/panel/templates``). This wrapper
injects exactly that token on every non-exempt POST so those tests keep
testing their own concern instead of CSRF itself.

CSRF enforcement itself (missing/invalid token, foreign Origin) is covered
by ``tests/test_panel_csrf.py`` with a raw ``AsyncClient``.
"""

from httpx import ASGITransport, AsyncClient

from app.panel.app import panel_app
from app.panel.csrf import csrf_token_for_cookie

_EXEMPT = ("/login", "/logout")


class PanelTestClient(AsyncClient):
    """AsyncClient that auto-injects the session-bound CSRF token."""

    async def post(self, url, *args, **kwargs):
        path = url if isinstance(url, str) else str(url)
        path = path.split("?")[0]
        if path not in _EXEMPT:
            token = csrf_token_for_cookie(self.cookies.get("panel_session", ""))
            data = kwargs.get("data")
            content = kwargs.get("content")
            if isinstance(data, dict):
                kwargs["data"] = {"csrf_token": token, **data}
            elif content is not None:
                body = content.decode() if isinstance(content, (bytes, bytearray)) else content
                from urllib.parse import parse_qsl, urlencode

                pairs = parse_qsl(body, keep_blank_values=True)
                if not any(k == "csrf_token" for k, _ in pairs):
                    pairs.append(("csrf_token", token))
                    kwargs["content"] = urlencode(pairs)
            elif "json" not in kwargs and "files" not in kwargs:
                kwargs["data"] = {"csrf_token": token}
        return await super().post(url, *args, **kwargs)


def panel_client() -> PanelTestClient:
    """A panel test client bound to the panel ASGI app."""
    return PanelTestClient(transport=ASGITransport(app=panel_app), base_url="http://test")
