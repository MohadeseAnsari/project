"""A deployed change must actually reach the operator's browser.

The panel's assets are served from stable URLs. Without a version, a browser
applies its own freshness rules and keeps the stylesheet it fetched days ago --
and for this panel the CSS *is* the design, so the operator sees the old
interface while the server has already changed, with nothing on screen to tell
the two apart.

So: versioned URLs, `no-store` on panel HTML, and a build stamp on every page.

The deeper point is the test method. Every earlier check used an in-process
ASGI client, which asks the application to render for itself and therefore
cannot see what a browser does with the result. That is how this survived a full
round of verification. The HTTP check here asks a real client what the server
actually sends.
"""

import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BASE_HTML = Path("app/panel/templates/base.html").read_text(encoding="utf-8")
APP = Path("app/panel/app.py").read_text(encoding="utf-8")


def test_the_stylesheet_is_cache_busted():
    assert 'href="/static/panel.css?v={{ asset_v() }}"' in BASE_HTML, (
        "panel.css must be requested with a content-derived version"
    )
    assert 'src="/static/htmx.min.js?v={{ asset_v() }}"' in BASE_HTML


def test_the_version_comes_from_the_files_not_the_clock():
    assert "hashlib.sha256()" in APP
    assert "lru_cache" in APP, "hashed once per process, not once per request"
    assert "read_bytes()" in APP
    assert "time.time()" not in APP.split("def _asset_version")[1][:600], (
        "a timestamp would bust the cache on every restart for no reason"
    )


def test_panel_html_is_never_cached():
    """A record is approved and a delivery fails while you are looking at it."""
    assert '"no-store, must-revalidate"' in APP
    assert 'startswith("text/html")' in APP, (
        "only HTML; the static mount manages its own headers"
    )


def test_the_globals_are_registered_after_the_functions_exist():
    """Registering one first crashes the panel on boot -- and takes every page
    with it, which is the worst possible failure for a diagnostic page."""
    for name in ("asset_v", "build_stamp"):
        assert APP.index(f"def _{name}") < APP.index(f'globals["{name}"]'), (
            f"{name} is registered before it is defined"
        )


@pytest.mark.asyncio
async def test_every_page_carries_the_build_stamp(session):
    """When a change "does not show up", this is the first thing to read."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import PanelUser, Role
    from app.panel.auth import hash_password
    from tests.panel_test_client import panel_client

    async def _admin(session: AsyncSession) -> None:
        role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
        if role is None:
            role = Role(name="admin", description="admin")
            session.add(role)
            await session.flush()
        session.add(PanelUser(username="stamp-admin", password_hash=hash_password("secret123"),
                              role_id=role.id, is_active=True))
        await session.commit()

    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "stamp-admin", "password": "secret123"},
                          follow_redirects=False)
        # Real pages in the CURRENT design: /deliveries and /categories are
        # 301 stubs onto a tab, so they are not pages to stamp.
        for path in ("/", "/records", "/categories", "/channels", "/models", "/users", "/audit"):
            page = await client.get(path)
            assert page.status_code == 200, path
            assert re.search(r"نسخهٔ [0-9a-f]{10}", page.text), f"{path} has no build stamp"
            assert f"/static/panel.css?v=" in page.text, f"{path} loads an unversioned stylesheet"


def test_the_whole_suite_of_templates_compiles():
    """A malformed template 500s the page, and a 500 is indistinguishable from
    a broken panel when you are the one looking at it."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/panel/templates"))
    broken = []
    for path in sorted(Path("app/panel/templates").glob("*.html")):
        try:
            env.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:                                  # noqa: BLE001
            broken.append(f"{path.name}: {exc}")
    assert not broken, "\n".join(broken)


def _http(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        headers = {k.lower(): v for k, v in response.headers.items()}
        return response.status, headers, response.read().decode("utf-8", "replace")


def test_the_running_server_sends_the_versioned_asset():
    """The check that would have caught it: ask the server over HTTP.

    A stale URL here is exactly what leaves the operator staring at an unchanged
    panel. Skips loudly when the server is not reachable, rather than passing
    silently.
    """
    try:
        status, headers, body = _http("http://127.0.0.1:8081/login")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"the panel is not running on 8081: {exc}")
    assert status == 200
    assert "no-store" in headers.get("cache-control", ""), (
        "HTML must be no-store, or the browser decides how stale it may be"
    )
    urls = re.findall(r'(?:href|src)="(/static/[^"]+)"', body)
    assert urls, "no assets referenced at all"
    for url in urls:
        assert "?v=" in url, f"{url} is requested without a version and will go stale"
    css_url = next(u for u in urls if "panel.css" in u)
    status, _headers, css = _http(f"http://127.0.0.1:8081{css_url}")
    assert status == 200, "the versioned URL must actually resolve"
    assert ".grid" in css, "the served asset is not the stylesheet"