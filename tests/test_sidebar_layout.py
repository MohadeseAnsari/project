"""The logged-in account must stay visible on every page (items 8 / 18).

The sidebar account block used to scroll off the bottom of the document
whenever a page was long, because ``.grid`` used ``min-height:100vh`` and
``<aside>`` sat in normal flow. These tests pin the fix: only the content
column scrolls, and the account block is outside it.
"""

import re
from pathlib import Path

import pytest

CSS = Path("app/panel/static/panel.css")
BASE = Path("app/panel/templates/base.html")


def _rule(selector: str) -> str:
    """The CSS rule block for a top-level selector, or '' when absent."""
    css = CSS.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(selector)}\{{(.*?)\}}", css, re.M | re.S)
    return match.group(1) if match else ""


def _decls(selector: str) -> dict[str, str]:
    body = _rule(selector)
    out = {}
    for chunk in body.split(";"):
        if ":" in chunk:
            key, _, value = chunk.partition(":")
            out[key.strip()] = value.strip()
    return out


@pytest.mark.parametrize("selector", [".grid", "aside", "main", "nav", ".side-foot"])
def test_the_rule_exists(selector: str):
    assert _decls(selector), f"{selector} must still be styled in panel.css"


def test_the_document_does_not_scroll_only_main_does():
    """The grid is viewport-height and clipped, so the page itself never scrolls."""
    grid = _decls(".grid")
    assert grid.get("height") == "100vh"
    assert grid.get("overflow") == "hidden"
    assert "min-height" not in grid, (
        "min-height lets the grid grow with the content, which is what pushed "
        "the sidebar account block off the bottom"
    )


def test_the_sidebar_is_a_fixed_height_flex_column():
    aside = _decls("aside")
    assert aside.get("height") == "100vh"
    assert aside.get("overflow") == "hidden"
    assert aside.get("flex-direction") == "column"


def test_the_content_column_is_the_scrolling_region():
    main = _decls("main")
    assert main.get("height") == "100vh"
    assert main.get("overflow-y") == "auto"


def test_the_nav_scrolls_instead_of_the_whole_sidebar():
    """Nav grows and scrolls; it must be allowed to shrink or it overflows."""
    nav = _decls("nav")
    assert nav.get("overflow-y") == "auto"
    assert nav.get("min-height") == "0"
    assert nav.get("flex") == "1 1 auto"


def test_the_account_block_cannot_be_squeezed_out():
    foot = _decls(".side-foot")
    assert foot.get("margin-top") == "auto", "pushed to the bottom of the sidebar"
    assert foot.get("flex") == "0 0 auto", "it must never shrink below its content"


def test_the_account_block_lives_outside_the_scrolling_nav():
    """It is a sibling of <nav> in the sidebar, so nav's scroll can't move it."""
    html = BASE.read_text(encoding="utf-8")
    nav = re.search(r"<nav\b[^>]*>.*?</nav>", html, re.S)
    assert nav, "sidebar nav must exist"
    assert "side-foot" not in nav.group(0), (
        "the account block must not be inside <nav>, which is the scrolling part"
    )
    assert "side-foot" in html
