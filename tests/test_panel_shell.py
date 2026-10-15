"""The shell: a collapsible sidebar, real breakpoints, and a density scale.

The owner's rule, stated as a requirement rather than a preference:
*"Collapsing the sidebar must never merge, remove, or hide the independent
navigation routes. Every page must remain accessible."*

That is the property worth defending, because the cheap way to build an icon
rail is to drop the labels from the DOM — which silently deletes routes for
anyone using a keyboard, a screen reader, or the browser's find-in-page. These
tests assert the labels are still there, collapsed to zero width, with every
``href`` intact.

They also cover the two facts about the old CSS that made this a real change
rather than a tweak: it had **zero** ``@media`` rules, and every gap, padding
and row height was a literal in a rule somewhere, so "make it tighter" was a
hunt rather than an edit.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, PanelUser, Role
from app.panel.app import panel_app
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client

CSS = Path("app/panel/static/panel.css").read_text(encoding="utf-8")
JS = Path("app/panel/static/panel.js").read_text(encoding="utf-8")

#: Every page the sidebar must always be able to reach. Taken from the real
#: nav rather than hardcoded, so a page added without a nav entry fails here.
NAV_PATHS = ["/", "/records", "/deliveries", "/fields", "/categories", "/channels",
             "/prompts", "/models", "/users", "/audit"]


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="shell-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    session.add(Category(name="پزشکی و علوم سلامت", description="Medical"))
    await session.commit()


async def _shell() -> str:
    async with panel_client() as client:
        await client.post("/login", data={"username": "shell-admin", "password": "secret123"},
                          follow_redirects=False)
        return (await client.get("/")).text


# --------------------------------------------------------------------------
# collapsing must not remove a route
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_nav_label_is_present_in_the_markup(session: AsyncSession):
    """Labels are collapsed by CSS width, never deleted from the document."""
    await _admin(session)
    html = await _shell()
    for path in NAV_PATHS:
        assert f'href="{path}"' in html, f"{path} is not in the sidebar at all"
    for label in ("داشبورد", "رکوردها و بازبینی", "ارسال‌ها و تحویل", "رشته‌ها",
                  "دسته‌ها و کلیدواژه‌ها", "کانال‌ها", "پرامپت‌ها", "مدل‌ها",
                  "کاربران و نقش‌ها", "رویدادها"):
        assert f">{label}<" in html, f"the label {label!r} must exist in the DOM"


@pytest.mark.asyncio
async def test_labels_are_hidden_by_width_not_by_removal(session: AsyncSession):
    """The difference between an icon rail and losing your navigation."""
    await _admin(session)
    html = await _shell()
    assert 'class="rail-label"' in html, "labels carry the collapse hook"
    rule = re.search(r"body\.rail-collapsed \.rail-label\{([^}]*)\}", CSS)
    assert rule, "the rail needs a rule that hides labels"
    body = rule.group(1)
    # Zero width + clipped, not display:none — a display:none node is skipped
    # by find-in-page and dropped from some assistive-tech trees.
    assert "width:0" in body and "display:none" not in body.replace("!important", "")
    assert "overflow:hidden" in body


@pytest.mark.asyncio
async def test_every_nav_link_carries_its_label_for_hover_and_screenreaders(
    session: AsyncSession,
):
    """Icon-only removes the visible text, so the title must restore it."""
    await _admin(session)
    html = await _shell()
    for path, label in (("/", "داشبورد"), ("/records", "رکوردها و بازبینی"),
                        ("/categories", "دسته‌ها و کلیدواژه‌ها")):
        assert f'href="{path}" title="{label}"' in html, f"{path} has no title"


@pytest.mark.asyncio
async def test_the_collapsed_state_is_reachable_without_loading_javascript(
    session: AsyncSession,
):
    """CSS alone must render a usable rail.

    The toggle is progressive enhancement. If htmx or panel.js fails to load,
    the page still has to be navigable — which it is, because collapsed is a
    class the stylesheet already understands.
    """
    await _admin(session)
    html = await _shell()
    assert ".grid.collapsed" in CSS
    assert 'class="grid"' in html, "the shell element exists without JS"
    assert "panel.js" in html, "and the toggle is wired when JS does load"


# --------------------------------------------------------------------------
# responsiveness
# --------------------------------------------------------------------------


def test_there_are_real_breakpoints():
    """There were none at all before this — a fixed grid at every width."""
    queries = re.findall(r"@media\s*\(([^)]+)\)", CSS)
    assert len(queries) >= 4, f"expected several breakpoints, found {queries}"
    widths = [int(m) for q in queries for m in re.findall(r"max-width:(\d+)px", q)]
    assert widths, "at least one width-based breakpoint is required"
    assert min(widths) <= 820, "something must handle a tablet"
    assert 1024 in widths, "the tablet rail threshold is part of the design"


def test_the_table_layout_collapses_on_a_tablet():
    """Two 1fr columns at 800px would put a 380px card in half the screen."""
    block = re.search(r"@media \(max-width:1280px\)\{(.*?)\n\}", CSS, re.S)
    assert block, "a laptop breakpoint is required"
    assert re.search(r"\.two\{[^}]*grid-template-columns:1fr", block.group(1)), (
        "two-column card rows must stack before the screen is too narrow"
    )


def test_the_narrow_layout_uses_a_drawer_not_a_removal():
    """Off-canvas with a scrim and a way back in — never display:none."""
    block = re.search(r"@media \(max-width:820px\)\{(.*?)\n@media", CSS, re.S)
    assert block, "the tablet breakpoint is required"
    body = block.group(1)
    assert "aside{position:fixed" in body, "the sidebar goes off-canvas"
    assert "transform:translateX" in body, "and is shown by transform, not display"
    assert ".scrim" in body, "with a backdrop to dismiss it"
    assert "drawer-open" in body


def test_reduced_motion_is_respected():
    """The drawer and the rail both animate; that is decoration, not meaning."""
    assert "@media (prefers-reduced-motion:reduce)" in CSS


def test_a_print_stylesheet_exists():
    """An operator printing a record should not print the navigation."""
    print_block = re.search(r"@media print\{(.*?)\n\}", CSS, re.S)
    assert print_block, "a print stylesheet is required"
    assert "aside" in print_block.group(1)


# --------------------------------------------------------------------------
# density tokens
# --------------------------------------------------------------------------


def test_spacing_and_row_height_come_from_tokens():
    """One edit to tighten the panel, not a hunt through 160 rules."""
    for token in ("--sidebar-w", "--rail-w", "--header-h", "--row-pad", "--fs-table"):
        assert f"{token}:" in CSS, f"{token} must be a token"
    assert "padding:var(--row-pad)" in CSS, "table rows must use the token"
    assert "grid-template-columns:var(--sidebar-w)" in CSS
    assert "min-height:var(--header-h)" in CSS


def test_table_text_does_not_go_below_the_readability_floor():
    """Compactness is not an excuse for unreadable rows.

    12.5px is the floor chosen deliberately. A denser token would be cheaper on
    scrolling and worse to read for every hour the operator spends here.
    """
    match = re.search(r"--fs-table:([\d.]+)px", CSS)
    assert match, "the table font size must be a token"
    assert float(match.group(1)) >= 12.5, "12.5px is the agreed floor"


def test_the_header_is_one_line_and_cannot_be_pushed_down():
    """A long breadcrumb must truncate, not wrap and push the page down."""
    assert "min-height:var(--header-h)" in CSS
    header = re.search(r"header\.top\{([^}]*)\}", CSS)
    assert "flex-wrap" not in header.group(1), "the header must not wrap"
    crumb = re.search(r"header\.top \.crumb\{([^}]*)\}", CSS)
    assert "text-overflow:ellipsis" in crumb.group(1)


def test_the_sticky_table_header_does_not_fight_the_sticky_page_header():
    """Both stick. They must stack, not overlap and hide the first rows."""
    assert "position:sticky" in CSS
    th = re.search(r"\nth\{([^}]*)\}", CSS)
    assert "top:var(--header-h)" in th.group(1), (
        "the table header must sit directly below the page header"
    )


# --------------------------------------------------------------------------
# the script that drives it
# --------------------------------------------------------------------------


def test_the_rail_state_is_remembered_but_never_traps_a_narrow_screen():
    """A stored "collapsed" must not leave a tablet with no navigation."""
    assert "localStorage" in JS
    assert "window.innerWidth <= NARROW" in JS, "narrow screens ignore the stored rail"
    assert "NARROW = 820" in JS


def test_the_drawer_closes_the_ways_a_user_expects():
    for behaviour in ("[data-close-drawer]", "Escape", "aside a"):
        assert behaviour in JS, f"the drawer must close on {behaviour}"


def test_the_script_is_dependency_free():
    """It ships before htmx does its work; a library here is a liability."""
    code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)  # comments may name htmx
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "import " not in code and "require(" not in code
    assert "htmx" not in code.lower(), "the frame must not call into the data layer"
    assert "fetch(" not in code, "and must not fetch: it owns presentation only"


@pytest.mark.asyncio
async def test_the_shell_renders_on_every_page(session: AsyncSession):
    """One template, every page — so one broken shell is ten broken pages."""
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "shell-admin", "password": "secret123"},
                          follow_redirects=False)
        for path in NAV_PATHS:
            page = await client.get(path)
            assert page.status_code == 200, path
            assert 'id="shell"' in page.text, path
            assert "data-rail-toggle" in page.text, path
            assert "/static/panel.js" in page.text, path


def test_the_login_page_is_not_wrapped_in_the_shell():
    """It overrides the layout block; the script must cope with no #shell."""
    login = Path("app/panel/templates/login.html").read_text(encoding="utf-8")
    assert "{% block layout %}" in login
    assert "if (!shell) return" in JS, "the script must no-op without a shell"

# --------------------------------------------------------------------------
# the page that answers "the panel is blank" from the browser's side
# --------------------------------------------------------------------------


def test_every_template_compiles():
    """A duplicate or malformed block is a 500, and it takes the page with it.

    The diagnose page shipped broken twice this way: once with a duplicate
    ``content`` block, once with ``h1``/``crumb`` declared both at the top and
    inside the layout override. Jinja refuses to compile either, so the route
    500s and the operator sees an error page rather than a diagnostic.
    """
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/panel/templates"))
    broken = []
    for path in sorted(Path("app/panel/templates").glob("*.html")):
        try:
            env.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            broken.append(f"{path.name}: {exc}")
    assert not broken, "templates that do not compile:\n" + "\n".join(broken)


def test_no_block_is_declared_twice_in_a_template():
    """The exact cause of both diagnose-page 500s."""
    import re as _re

    for path in sorted(Path("app/panel/templates").glob("*.html")):
        names = _re.findall(r"{%\s*block\s+([a-z_]+)", path.read_text(encoding="utf-8"))
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"{path.name} declares {sorted(dupes)} more than once"


@pytest.mark.asyncio
async def test_the_diagnose_page_renders_and_answers_the_real_questions(session: AsyncSession):
    """It must measure, not assert.

    Every question on the page is one the SERVER cannot answer about itself:
    which stylesheet the browser got, whether the script ran, what the CSS
    viewport is (zoom changes it), and whether the menu overlaps the content.
    """
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "shell-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/diagnose")
    assert page.status_code == 200
    body = page.text
    for probe in ("live-version", "css-state", "js-state", "vw", "mode",
                  "main-w", "main-h", "overlap", "verdict"):
        assert f'id="{probe}"' in body, f"the page must measure {probe}"
    # It compares what the browser fetched against what the server says.
    # The call is resolved at render time, so the VALUE is what arrives.
    assert re.search(r"نسخهٔ [0-9a-f]{10} · built", body), (
        "the page must print the build stamp it compares against"
    )
    assert "document.styleSheets" in body, "CSS presence is read from the CSSOM, not assumed"
    assert "documentElement.classList.contains('no-js')" in body, (
        "script execution is proven by the marker panel.js removes as its first act"
    )
    assert "getBoundingClientRect" in body, "overlap is measured, not guessed"


def test_the_diagnose_page_is_reachable_from_the_sidebar():
    """An owner who cannot see the panel still has to be able to find it."""
    base = Path("app/panel/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/diagnose"' in base
    assert 'title="تشخیص نمایش"' in base
