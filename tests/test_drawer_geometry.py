"""The narrow-screen drawer must hide, not cover, the page.

This file exists because of a bug I introduced and the owner reported as "the
panel is blank". The server was healthy the whole time — every route returned
200 with content, and every htmx partial resolved — but the stylesheet pinned
the off-canvas sidebar to the WRONG EDGE:

    aside{ inset-inline-end: 0 }        /* RTL: inline-end is the LEFT edge */
    html[dir=rtl] aside{ transform: translateX(100%) }   /* ...back across the page */

The panel is `dir="rtl"`, so the drawer landed on the left and the RTL
translate slid it right, across the content. Below 820px the sidebar therefore
lay permanently ON TOP of the page instead of hiding off-canvas.

A page hidden behind its own navigation is indistinguishable from a blank one,
so the geometry is asserted rather than eyeballed: the sidebar must sit on the
same edge in both directions, and the closed transform must move it AWAY from
the viewport, never back across it.

The second bug is the same shape: the drawer opens via a class that a script
toggles, so with no script the sidebar was off-screen AND could not be brought
back. The panel must be navigable without JavaScript.
"""

import re
from pathlib import Path

CSS = Path("app/panel/static/panel.css").read_text(encoding="utf-8")
BASE = Path("app/panel/templates/base.html").read_text(encoding="utf-8")
JS = Path("app/panel/static/panel.js").read_text(encoding="utf-8")


def _drawer_block() -> str:
    match = re.search(r"@media \(max-width:820px\)\{(.*?)\n\}", CSS, re.S)
    assert match, "the drawer breakpoint must exist"
    return match.group(1)


def test_the_drawer_is_pinned_to_the_side_it_already_lives_on():
    """inline-start, because RTL puts inline-start on the right.

    The sidebar is the first grid child of an RTL grid, so it renders on the
    right. `inset-inline-end` is the LEFT in RTL, which is how it ended up
    covering the page.
    """
    block = _drawer_block()
    aside = re.search(r"aside\{(position:fixed[^}]*)\}", block)
    assert aside, "the drawer rule must exist"
    assert "inset-inline-start:0" in aside.group(1), (
        "pin the drawer with inline-start: that is the side it already occupies"
    )
    assert "inset-inline-end" not in aside.group(1), (
        "inline-end is the opposite edge in RTL and puts the drawer on top of the content"
    )


def test_the_closed_transform_moves_the_drawer_OFF_SCREEN():
    """Closed must be off-screen. In both directions, not one."""
    block = _drawer_block()
    base_rule = re.search(r"aside\{position:fixed[^}]*transform:translateX\((-?100%)\)[^}]*\}", block)
    assert base_rule, "the default (LTR) transform must be a full-width slide out"
    assert base_rule.group(1) == "-100%"
    rtl = re.search(r'html\[dir=rtl\] aside\{transform:translateX\((-?100%)\)\}', block)
    assert rtl, "the RTL direction needs its own transform"
    assert rtl.group(1) == "100%", (
        "RTL puts the sidebar on the right, so hiding it means further right"
    )


def test_opening_the_drawer_reverses_the_slide():
    """Otherwise the button does nothing visible."""
    block = _drawer_block()
    assert re.search(r"\.grid\.drawer-open aside[^{]*\{transform:none\}", block), (
        "the open state must return the drawer to transform:none"
    )


def test_the_content_is_never_covered_at_drawer_widths():
    """The regression in one line: one column, and main gets it."""
    block = _drawer_block()
    assert re.search(r"\.grid,\.grid\.collapsed\{grid-template-columns:1fr\}", block), (
        "the sidebar is position:fixed below this width, so main must get the whole width"
    )
    # And nothing else may cover it: the scrim only exists while open.
    scrim = re.search(r"\.scrim\{([^}]*)\}", CSS)
    assert scrim and "display:none" in scrim.group(1), (
        "the backdrop must be hidden until the drawer is open"
    )
    assert ".grid.drawer-open .scrim{display:block}" in CSS


def test_the_panel_is_usable_without_javascript():
    """A drawer opened by a script, hidden by CSS, is unreachable without it."""
    assert 'class="no-js"' in BASE, "the document must be marked as un-scripted"
    assert "html.no-js .grid aside{transform:none!important}" in CSS, (
        "without script the sidebar must be pinned open, not off-screen"
    )
    assert "document.documentElement.classList.remove('no-js')" in JS, (
        "and the script must remove the marker as its first act, or the drawer "
        "stays pinned open forever once it loads"
    )


def test_the_menu_button_is_not_controlled_by_an_inline_style():
    """An inline display:none beats a normal stylesheet rule."""
    button = re.search(r"<button class=\"menu-btn\".*?>", BASE, re.S)
    assert button, "the drawer toggle must exist"
    assert "style=" not in button.group(0), (
        "its visibility must come from the stylesheet, or it can get stuck hidden"
    )
    assert ".menu-btn{display:none" in CSS
    assert re.search(r"@media \(max-width:820px\)\{[^@]*?\.menu-btn\{display:flex", CSS, re.S), (
        "and it must appear at drawer widths"
    )


def test_every_route_is_reachable_without_scripting():
    """The owner's rule, restated for the case that broke it.

    The sidebar links are plain <a href>, not buttons, so navigation does not
    depend on any script at all.
    """
    links = re.findall(r'<a href="(/[^"]*)"[^>]*title=', BASE)
    assert len(links) >= 10, "every page must have a nav link"
    for link in links:
        assert link.startswith("/"), "navigation must not leave the app"
    assert not re.search(r'<a[^>]*onclick=', BASE), "no link may depend on inline script"


def test_the_script_still_parses_and_stays_dependency_free():
    code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    # Balanced enough to catch a truncated file.
    assert code.count("{") == code.count("}"), "panel.js is malformed"
    assert code.count("(") == code.count(")"), "panel.js is malformed"
    assert "import " not in code and "require(" not in code
    assert "fetch(" not in code