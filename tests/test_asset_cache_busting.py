"""A deployed change must actually reach the operator's browser.

This file exists because of a real miss. The shell redesign, the page split and
the fields pagination were all verified with an in-process ASGI client, which
proves the *code* is right — and the owner still saw an unchanged panel, because
``/static/panel.css`` is a stable URL and their browser kept serving the CSS it
had fetched days earlier.

The failure was invisible to every check that existed, because all of them
asked the application for its own output. Asking a real HTTP client what the
server sends is a different question, and it is the one that matters.

Two things are pinned:

* the CSS and JS carry a content-derived ``?v=``, so a changed file is a
  different URL while an unchanged one stays cacheable;
* panel HTML is ``no-store``, because an admin page whose data changes
  underneath it must never be served from a cache.
"""

import re
import urllib.error
import urllib.request

import pytest
from pathlib import Path

BASE = Path("app/panel/templates/base.html").read_text(encoding="utf-8")
APP = Path("app/panel/app.py").read_text(encoding="utf-8")


def test_the_stylesheet_is_cache_busted():
    """A stable URL for a changing file is how the owner sees a stale panel."""
    assert 'href="/static/panel.css?v={{ asset_v }}"' in BASE, (
        "panel.css must be requested with a content-derived version"
    )
    assert 'src="/static/panel.js?v={{ asset_v }}"' in BASE, "and so must panel.js"


def test_the_version_is_derived_from_the_files_not_the_clock():
    """A timestamp would bust the cache on every restart for no reason."""
    assert "hashlib.sha256()" in APP
    assert '"panel.css", "panel.js"' in APP
    assert "lru_cache" in APP, "hashed once per process, not once per request"
    assert "read_bytes()" in APP


def test_the_third_party_bundle_is_left_alone():
    """htmx.min.js is vendored and unchanged; versioning it is churn."""
    assert 'src="/static/htmx.min.js" defer></script>' in BASE


def test_panel_html_is_never_cached():
    """A record is approved, a delivery fails — the page behind you goes stale."""
    assert '"no-store, must-revalidate"' in APP
    assert 'startswith("text/html")' in APP, (
        "only HTML; the static mount manages its own headers"
    )


def test_every_page_gets_the_version():
    """One layout, so a miss on one template is a miss on all ten."""
    assert BASE.count("{{ asset_v }}") == 2, "stylesheet and script"


# --------------------------------------------------------------------------
# what a REAL client receives — not what the app says it would
# --------------------------------------------------------------------------


def _http(url: str) -> tuple[int, dict, str]:
    """Returns headers lower-cased: HTTP header names are case-insensitive and
    ``dict(response.headers)`` would otherwise make a lookup on ``Cache-Control``
    miss a server that sent ``cache-control`` correctly."""
    with urllib.request.urlopen(url, timeout=10) as response:
        headers = {k.lower(): v for k, v in response.headers.items()}
        return response.status, headers, response.read().decode("utf-8", "replace")


def test_the_running_server_serves_the_versioned_asset():
    """The check that would have caught this.

    It asks the deployed server over HTTP what it actually sends, rather than
    asking the application to render for itself. A stale URL here is exactly
    what leaves the owner staring at an unchanged panel.
    """
    try:
        status, headers, body = _http("http://127.0.0.1:8081/login")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"the panel is not running on 8081: {exc}")
    assert status == 200
    assert "no-store" in headers.get("cache-control", ""), (
        "HTML must be no-store, or the browser decides how stale it may be"
    )
    for asset in re.findall(r'(?:href|src)="(/static/[^"]+)"', body):
        assert asset.endswith(".js") or "?v=" in asset or "htmx" in asset, (
            f"{asset} is requested without a version and will go stale"
        )

    css_urls = re.findall(r'href="(/static/panel\.css[^"]*)"', body)
    assert css_urls, "the stylesheet link must be present"
    assert "?v=" in css_urls[0], "and must carry the content version"

    # And the versioned URL must actually resolve, with real CSS in it.
    status, _headers, css = _http(f"http://127.0.0.1:8081{css_urls[0]}")
    assert status == 200, "the versioned URL must actually resolve"
    assert ".grid" in css, "the served asset is not the stylesheet"
    assert "@media" in css, "the served CSS is the one with the breakpoints in it"