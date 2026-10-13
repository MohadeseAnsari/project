"""Panel layering is ENFORCED, not documented (A1/A2).

The panel is a client of the backend: it may talk to the service layer and to
shared pure logic (messaging/news/fields helpers), never to the ORM or to the
session factory. These tests fail the moment someone adds a back door.

Two rules:
1. No module under ``app/panel/`` may import ``app.db.*`` (``async_session``,
   the engine, or any ORM model) — the DB belongs to the services.
2. Every panel route handler must call at least one service function (or a
   shared pure helper) — no route may hide its own SQL.
"""

import ast
from pathlib import Path

import pytest

PANEL_DIR = Path("app/panel")

#: Modules the panel is allowed to reach for (services own the rest).
ALLOWED_PANEL_IMPORTS = {
    "app.config",
    "app.panel.auth",
    "app.panel.authz",
    "app.panel.csrf",
    "app.services",
    "app.services.audit",
    "app.services.errors",
    "app.services.channels",
    "app.services.deliveries",
    "app.services.fields",
    "app.services.models",
    "app.services.passwords",
    "app.services.prompts",
    "app.services.records",
    "app.services.transitions",
    "app.services.users",
    "app.news.generation",
    "app.news.prompts",
    "app.messaging.dispatcher",
    "app.messaging.formatting",
    "app.fields.classification",
    "app.fields.normalization",
}

FORBIDDEN_PREFIXES = ("app.db",)


def _imported_modules(path: Path) -> set[str]:
    """Every ``app.*`` module imported by a file (top-level or local)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize(
    "path", sorted(PANEL_DIR.glob("*.py")), ids=lambda p: p.name
)
def test_panel_modules_do_not_import_the_db_layer(path: Path):
    """No panel module may import app.db.*, async_session, or an ORM model."""
    offenders = {
        module
        for module in _imported_modules(path)
        if module.startswith(FORBIDDEN_PREFIXES)
    }
    assert not offenders, (
        f"{path} imports the database layer directly ({sorted(offenders)}). "
        "Panel routes must go through app/services/* instead."
    )


@pytest.mark.parametrize(
    "path", sorted(PANEL_DIR.glob("*.py")), ids=lambda p: p.name
)
def test_panel_modules_only_import_allowed_layers(path: Path):
    """Panel imports stay inside the allowed layers (no surprise coupling)."""
    offenders = set()
    for module in _imported_modules(path):
        if not module.startswith("app."):
            continue
        if module.startswith(FORBIDDEN_PREFIXES):
            offenders.add(module)
        elif module not in ALLOWED_PANEL_IMPORTS:
            offenders.add(module)
    assert not offenders, (
        f"{path} imports modules outside the panel's allowed layers: {sorted(offenders)}"
    )


def _route_endpoints():
    """Every registered panel route with its handler function.

    Only routes defined by this project are returned: FastAPI's generated
    ``/openapi.json`` and static mounts are framework plumbing, not handlers
    under our layering rules.
    """
    from app.panel.app import panel_app

    for route in panel_app.routes:
        handler = getattr(route, "endpoint", None)
        if handler is None:
            continue
        if getattr(handler, "__module__", "") != "app.panel.app":
            continue
        yield route.path, sorted(getattr(route, "methods", []) or []), handler


def test_panel_routes_exist():
    """Sanity: the panel registers its pages and mutations."""
    paths = {path for path, _methods, _handler in _route_endpoints()}
    for expected in ("/login", "/", "/records", "/deliveries", "/fields", "/channels", "/models"):
        assert expected in paths, f"missing panel route {expected}"


@pytest.mark.parametrize(
    "path,methods,handler",
    [(p, m, h) for p, m, h in _route_endpoints() if "static" not in p],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_panel_route_calls_a_service(path, methods, handler):
    """Each panel route handler delegates to the service layer.

    A handler that touches no service owns too much: either its data access
    must move into ``app/services``, or it is pure HTTP plumbing (login page,
    logout, exception handlers) and is exempted by name.
    """
    name = handler.__name__
    if name in {
        "logout",  # clears a signed cookie; no data access
        "login_page",  # anonymous GET, renders the form
        "lifespan",  # startup hook
    }:
        return
    source = ast.parse(_source_of(handler))
    called: set[str] = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Attribute) and node.attr:
            called.add(node.attr)
        elif isinstance(node, ast.Name):
            called.add(node.id)
    service_markers = [n for n in called if n.endswith("_service")]
    assert service_markers, (
        f"panel route {name} ({' '.join(methods)} {path}) calls no service function; "
        "move its data access into app/services/*"
    )


def _source_of(handler) -> str:
    import inspect

    return inspect.getsource(handler)