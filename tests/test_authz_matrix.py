"""Authorization in ONE place (A4): a single guard, applied everywhere.

``app/panel/authz.py`` is the only place that answers "may this role do
this?". Routes depend on it instead of re-implementing role checks, and this
module pins the resulting role x action matrix and proves every mutating
route is actually covered by it.
"""

import inspect

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.connection import async_session
from app.db.models import AuditLog, Channel, Delivery, Main, PanelUser, Role
from app.panel.app import panel_app
from app.panel.authz import Action, can, require
from app.services.passwords import hash_password
from tests.panel_test_client import panel_client

#: role -> allowed actions. This table IS the policy (see docs/admin-panel.md).
EXPECTED_MATRIX = {
    "viewer": {Action.VIEW_RECORDS},
    "editor": {
        Action.VIEW_RECORDS,
        Action.REVIEW_RECORD,
        Action.RETRY_DELIVERY,
        Action.SUBMIT_RECORD,
    },
    "admin": {
        Action.VIEW_RECORDS,
        Action.REVIEW_RECORD,
        Action.RETRY_DELIVERY,
        Action.SUBMIT_RECORD,
        Action.MANAGE_CONFIG,
        Action.MANAGE_USERS,
        Action.VIEW_AUDIT,
    },
}


# --------------------------------------------------------------------------
# the matrix itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role,allowed", sorted(EXPECTED_MATRIX.items()))
def test_role_matrix_matches_policy(role, allowed):
    """Every role's permission set is exactly what the docs promise."""
    actual = {a for a in Action if can(role, a)}
    assert actual == allowed, f"{role} drifted from the documented matrix"


def test_viewer_is_read_only():
    """A viewer may look, never touch."""
    assert can("viewer", Action.VIEW_RECORDS)
    for action in (
        Action.REVIEW_RECORD, Action.RETRY_DELIVERY, Action.SUBMIT_RECORD,
        Action.MANAGE_CONFIG, Action.MANAGE_USERS, Action.VIEW_AUDIT,
    ):
        assert not can("viewer", action), f"viewer must not be allowed {action}"


def test_editor_cannot_touch_configuration_or_users():
    """Editors review; they do not manage channels, models or accounts."""
    assert can("editor", Action.REVIEW_RECORD)
    for action in (Action.MANAGE_CONFIG, Action.MANAGE_USERS, Action.VIEW_AUDIT):
        assert not can("editor", action)


def test_unknown_role_has_no_permissions():
    """Fail closed: an unrecognised role gets nothing."""
    assert not can("nobody", Action.VIEW_RECORDS)
    assert not can(None, Action.REVIEW_RECORD)


def test_require_raises_with_a_stable_code():
    """The guard raises the domain code routes already map to 403."""
    from app.services.errors import PermissionDenied

    require("viewer", Action.VIEW_RECORDS)  # permitted: silent
    with pytest.raises(PermissionDenied) as exc:
        require("viewer", Action.REVIEW_RECORD)
    assert exc.value.code == "forbidden"
    # And the guard is silent for a permitted action.
    require("admin", Action.MANAGE_USERS)


# --------------------------------------------------------------------------
# routes are covered by the guard
# --------------------------------------------------------------------------


def _mutating_routes():
    """(path, action) for every panel POST route, from the guard mapping."""
    from app.panel.app import panel_app

    routes = []
    for route in panel_app.routes:
        methods = set(getattr(route, "methods", []) or [])
        if "POST" not in methods:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or getattr(endpoint, "__module__", "") != "app.panel.app":
            continue
        if route.path in ("/login", "/logout"):  # pre-session, no role yet
            continue
        routes.append((route.path, endpoint))
    return routes


def test_every_post_route_declares_an_action():
    """No mutating panel route may be left without an authorization decision.

    The route must either call ``require``/``can`` from ``app.panel.authz`` or
    be explicitly listed with its action below.
    """
    from app.panel.app import GUARDED_ROUTES, SELF_SERVICE_ROUTES

    undeclared = []
    for path, endpoint in _mutating_routes():
        source = inspect.getsource(endpoint)
        uses_guard = (
            "_require_role(" in source
            or "_require_admin_or_403" in source
            or "Action." in source
        )
        if uses_guard or path in GUARDED_ROUTES or path in SELF_SERVICE_ROUTES:
            continue
        undeclared.append(f"{path} ({endpoint.__name__})")
    assert not undeclared, f"POST routes with no authorization guard: {undeclared}"


@pytest.mark.parametrize("path,endpoint", _mutating_routes(), ids=[p for p, _ in _mutating_routes()])
def test_guarded_routes_check_the_right_action(path, endpoint):
    """Each route's guard names the action it protects."""
    from app.panel.app import GUARDED_ROUTES, SELF_SERVICE_ROUTES

    source = inspect.getsource(endpoint)
    expected = GUARDED_ROUTES.get(path)
    if path in SELF_SERVICE_ROUTES:
        assert "require_login" in source, f"{path} must still require a signed-in user"
        return
    if expected is not None:
        assert f"Action.{expected}" in source, f"{path} should check Action.{expected}"
    else:
        assert "_require_role(" in source or "Action." in source


# --------------------------------------------------------------------------
# behaviour over HTTP: the matrix is real, not just documented
# --------------------------------------------------------------------------


async def _user(session: AsyncSession, username: str, role_name: str) -> PanelUser:
    role = (await session.execute(select(Role).where(Role.name == role_name))).scalar_one_or_none()
    if role is None:
        role = Role(name=role_name, description=role_name)
        session.add(role)
        await session.flush()
    user = PanelUser(username=username, password_hash=hash_password("secret123"),
                     role_id=role.id, is_active=True)
    session.add(user)
    await session.commit()
    return user


async def _login_as(role_name: str):
    client = panel_client()
    await client.__aenter__()
    response = await client.post(
        "/login", data={"username": f"matrix-{role_name}", "password": "secret123"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    return client


@pytest.fixture(autouse=True)
def _clean_login_guard():
    """Reset the process-global login counters (httpx reports 127.0.0.1)."""
    from app.panel.auth import clear_login_failures

    for ip in ("127.0.0.1", "testclient", "unknown"):
        clear_login_failures(ip)
    yield
    for ip in ("127.0.0.1", "testclient", "unknown"):
        clear_login_failures(ip)


@pytest_asyncio.fixture
async def matrix_users(session: AsyncSession):
    for role_name in ("viewer", "editor", "admin"):
        await _user(session, f"matrix-{role_name}", role_name)
    return True


@pytest.mark.asyncio
async def test_viewer_blocked_from_every_mutation_over_http(session: AsyncSession, matrix_users):
    """A viewer gets 403 on review, delivery retry and management alike."""
    record = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(record)
    await session.commit()
    channel = Channel(platform="bale", chat_id=9930001, username="@c", is_active=True)
    session.add(channel)
    await session.commit()
    delivery = Delivery(main_id=record.id, channel_id=channel.id, status="failed")
    session.add(delivery)
    await session.commit()

    client = await _login_as("viewer")
    try:
        assert (await client.get("/records")).status_code == 200, "viewing is allowed"
        assert (await client.get("/users")).status_code == 403
        assert (await client.get("/audit")).status_code == 403
        assert (await client.post(f"/records/{record.id}/review",
                                  data={"action": "approve"}, follow_redirects=False)).status_code == 403
        assert (await client.post(f"/deliveries/{delivery.id}/retry", follow_redirects=False)).status_code == 403
        assert (await client.post(f"/deliveries/{delivery.id}/cancel", follow_redirects=False)).status_code == 403
    finally:
        await client.__aexit__(None, None, None)
    # Nothing changed.
    session.expire_all()
    await session.refresh(record)
    assert record.status == "review"
    await session.refresh(delivery)
    assert delivery.status == "failed"


@pytest.mark.asyncio
async def test_editor_can_review_but_not_manage(session: AsyncSession, matrix_users):
    """Editor: review works, users/audit/config return 403."""
    record = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(record)
    await session.commit()

    client = await _login_as("editor")
    try:
        assert (await client.get("/users")).status_code == 403
        assert (await client.get("/audit")).status_code == 403
        assert (await client.post("/users", data={"username": "x", "password": "longenough", "role": "viewer"},
                                  follow_redirects=False)).status_code == 403
        response = await client.post(
            f"/records/{record.id}/review", data={"action": "save_draft", "title": "T2"},
            follow_redirects=False,
        )
        assert response.status_code == 302, "editors may edit record text"
    finally:
        await client.__aexit__(None, None, None)
    async with async_session() as check:
        fresh = await check.get(Main, record.id)
        assert fresh.output_json["title"] == "T2", "the editor's edit was saved"


@pytest.mark.asyncio
async def test_admin_can_everything_matrix_action(session: AsyncSession, matrix_users):
    """Admin: users and audit pages are reachable."""
    client = await _login_as("admin")
    try:
        assert (await client.get("/users")).status_code == 200
        assert (await client.get("/audit")).status_code == 200
        assert (await client.get("/channels")).status_code == 200
        assert (await client.get("/models")).status_code == 200
    finally:
        await client.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_login_is_exempt_from_the_role_guard(session: AsyncSession):
    """The login page itself is public (there is no role yet)."""
    async with AsyncClient(transport=ASGITransport(app=panel_app), base_url="http://test") as client:
        assert (await client.get("/login")).status_code == 200
        assert (await client.get("/", follow_redirects=False)).status_code == 302


@pytest.mark.asyncio
async def test_audit_records_the_actor_for_permitted_actions(session: AsyncSession, matrix_users):
    """A permitted action writes an audit row naming the acting user."""
    record = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(record)
    await session.commit()
    client = await _login_as("editor")
    try:
        await client.post(f"/records/{record.id}/review", data={"action": "approve"},
                          follow_redirects=False)
    finally:
        await client.__aexit__(None, None, None)
    actor = (await session.execute(
        select(AuditLog.user_id).where(AuditLog.action == "approve")
    )).scalars().all()
    editor = (await session.execute(
        select(PanelUser).where(PanelUser.username == "matrix-editor")
    )).scalar_one()
    assert actor == [editor.id]