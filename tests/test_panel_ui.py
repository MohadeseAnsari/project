"""Smoke tests for the server-rendered review panel (templates + one HTMX part)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.panel.app import panel_app
from app.panel.auth import hash_password


async def _user(session: AsyncSession, username: str = "panel-tester") -> None:
    """Create an admin user directly (login is exercised over HTTP below)."""
    from sqlalchemy import select

    from app.db.models import PanelUser, Role

    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(
        PanelUser(
            username=username,
            password_hash=hash_password("secret123"),
            role_id=role.id,
            is_active=True,
        )
    )
    await session.commit()


async def _user_with_role(session: AsyncSession, username: str, role_name: str) -> None:
    """Create a user with the given role (viewer/editor split is tested below)."""
    from sqlalchemy import select

    from app.db.models import PanelUser, Role

    role = (await session.execute(select(Role).where(Role.name == role_name))).scalar_one_or_none()
    if role is None:
        role = Role(name=role_name, description=role_name)
        session.add(role)
        await session.flush()
    session.add(
        PanelUser(
            username=username,
            password_hash=hash_password("secret123"),
            role_id=role.id,
            is_active=True,
        )
    )
    await session.commit()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=panel_app), base_url="http://test")


@pytest.mark.asyncio
async def test_login_template_is_persian_rtl_with_local_htmx():
    """Login page renders Persian RTL with locally vendored HTMX (no CDN)."""
    async with _client() as client:
        response = await client.get("/login")
    assert response.status_code == 200
    html = response.text
    assert 'dir="rtl"' in html
    assert "ورود به کنسول ایران‌داک" in html
    assert "/static/htmx.min.js" in html
    assert "unpkg.com" not in html
    assert "/static/panel.css" in html


@pytest.mark.asyncio
async def test_dashboard_requires_login(session: AsyncSession):
    """Anonymous browsers are sent to /login instead of seeing data."""
    async with _client() as client:
        response = await client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_and_htmx_records_table(session: AsyncSession):
    """Full login over HTTP, then the HTMX records partial returns a table."""
    await _user(session)
    async with _client() as client:
        login = await client.post(
            "/login", data={"username": "panel-tester", "password": "secret123"},
            follow_redirects=False,
        )
        assert login.status_code == 302
        assert "panel_session" in login.cookies
        dashboard = await client.get("/")
        assert dashboard.status_code == 200
        assert "داشبورد" in dashboard.text
        # The one HTMX endpoint test: partial table HTML, not JSON.
        table = await client.get("/records/table", params={"page": 1, "page_size": 5})
        assert table.status_code == 200
        assert "<table>" in table.text
        assert "رکورد" in table.text


async def _login(client: AsyncClient, username: str) -> None:
    """Log in over HTTP and keep the session cookie on the client."""
    response = await client.post(
        "/login", data={"username": username, "password": "secret123"},
        follow_redirects=False,
    )
    assert response.status_code == 302


@pytest.mark.asyncio
async def test_users_page_is_admin_only(session: AsyncSession):
    """Users management renders for admins, 403 for editors (matrix page)."""
    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    async with _client() as client:
        await _login(client, "panel-admin")
        page = await client.get("/users")
        assert page.status_code == 200
        assert "کاربران کنسول" in page.text
        assert "ماتریس دسترسی نقش‌ها" in page.text
    async with _client() as editor:
        await _login(editor, "panel-editor")
        assert (await editor.get("/users")).status_code == 403
        assert (await editor.get("/audit")).status_code == 403


@pytest.mark.asyncio
async def test_audit_page_lists_trail(session: AsyncSession):
    """Audit trail renders the immutable event table for admins."""
    await _user(session, "panel-admin")
    async with _client() as client:
        await _login(client, "panel-admin")
        page = await client.get("/audit")
        assert page.status_code == 200
        assert "ردپای تغییرات" in page.text


@pytest.mark.asyncio
async def test_viewer_is_read_only(session: AsyncSession):
    """Viewers see pages but every review mutation returns 403."""
    from app.db.models import Main

    await _user_with_role(session, "panel-viewer", "viewer")
    row = Main(
        username="tester", title="Thesis", abstract="Abstract", field="پزشکی",
        university="Tehran University", author="Author", supervisor="Supervisor",
        status="review", output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    async with _client() as client:
        await _login(client, "panel-viewer")
        assert (await client.get("/records")).status_code == 200
        assert (await client.get(f"/records/{row.id}")).status_code == 200
        denied = await client.post(f"/records/{row.id}/review", data={"action": "approve"})
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_panel_submit_creates_ready_job(session: AsyncSession):
    """Panel submit inserts the same ready row the Worker polls for."""
    from app.db.models import Main

    await _user(session, "panel-admin")
    async with _client() as client:
        await _login(client, "panel-admin")
        response = await client.post(
            "/submit",
            data={
                "username": "tester", "title": "Panel Thesis", "abstract": "Abstract",
                "field": "پزشکی", "university": "Tehran University",
                "table_of_contents": "1. Intro", "author": "Author", "supervisor": "Supervisor",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"].startswith("/records/")
    row = (
        await session.execute(select(Main).where(Main.title == "Panel Thesis"))
    ).scalar_one()
    assert row.status == "ready"


@pytest.mark.asyncio
async def test_panel_regenerate_uses_worker_path(session: AsyncSession):
    """Regenerate re-runs generation and returns the record to review."""
    from app.db.models import AuditLog, Main

    await _user(session, "panel-admin")
    row = Main(
        username="tester", title="Thesis", abstract="Abstract", field="پزشکی",
        university="Tehran University", author="Author", supervisor="Supervisor",
        status="failed", error_message="old error",
        output_json={"title": "Old", "lead": "Old", "body": "Old"},
    )
    session.add(row)
    await session.commit()
    with patch("app.news.generation.generate_news") as mock_gen:
        mock_gen.return_value = AsyncMock(title="New", lead="New lead", body="New body")
        async with _client() as client:
            await _login(client, "panel-admin")
            response = await client.post(f"/records/{row.id}/regenerate", follow_redirects=False)
            assert response.status_code == 302
    await session.refresh(row)
    assert row.status == "review"
    assert row.output_json == {"title": "New", "lead": "New lead", "body": "New body"}
    assert row.error_message is None
    audits = (
        await session.execute(select(AuditLog).where(AuditLog.main_id == row.id))
    ).scalars().all()
    assert [a.action for a in audits] == ["regenerate"]


@pytest.mark.asyncio
async def test_panel_regenerate_marks_failed_on_provider_error(session: AsyncSession):
    """A failed regeneration parks the record as failed with the error text."""
    from app.db.models import Main

    await _user(session, "panel-admin")
    row = Main(
        username="tester", title="Thesis", abstract="Abstract", field="پزشکی",
        university="Tehran University", author="Author", supervisor="Supervisor",
        status="review", output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    with patch("app.news.generation.generate_news") as mock_gen:
        mock_gen.side_effect = RuntimeError("provider down")
        async with _client() as client:
            await _login(client, "panel-admin")
            response = await client.post(f"/records/{row.id}/regenerate", follow_redirects=False)
            assert response.status_code == 302
    await session.refresh(row)
    assert row.status == "failed"
    assert "provider down" in row.error_message


@pytest.fixture
def _clean_login_guard():
    """Isolate the process-global login counters between tests.

    httpx's ASGI transport reports the peer as 127.0.0.1, so every test
    shares one bucket — reset it (plus fallbacks) around each test.
    """
    from app.panel.auth import clear_login_failures

    for _ip in ("127.0.0.1", "testclient", "unknown"):
        clear_login_failures(_ip)
    yield
    for _ip in ("127.0.0.1", "testclient", "unknown"):
        clear_login_failures(_ip)


@pytest.mark.asyncio
async def test_failed_login_is_audited(session: AsyncSession, _clean_login_guard):
    """Wrong passwords return 401 and leave a login_failed audit row."""
    from sqlalchemy import select

    from app.db.models import AuditLog

    await _user(session, "panel-admin")
    async with _client() as client:
        response = await client.post(
            "/login", data={"username": "panel-admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 401
    actions = (
        await session.execute(select(AuditLog.action).where(AuditLog.action == "login_failed"))
    ).scalars().all()
    assert actions == ["login_failed"]


@pytest.mark.asyncio
async def test_login_locks_out_after_repeated_failures(
    session: AsyncSession, monkeypatch, _clean_login_guard
):
    """Reaching the cap returns 429; a later success still needs the lockout cleared."""
    from app.config import settings

    monkeypatch.setattr(settings, "panel_login_max_attempts", 3)
    monkeypatch.setattr(settings, "panel_login_window_minutes", 10)
    monkeypatch.setattr(settings, "panel_login_lockout_minutes", 15)
    await _user(session, "panel-admin")
    async with _client() as client:
        assert (await client.post("/login", data={"username": "panel-admin", "password": "bad1"})).status_code == 401
        assert (await client.post("/login", data={"username": "panel-admin", "password": "bad2"})).status_code == 401
        locked = await client.post("/login", data={"username": "panel-admin", "password": "bad3"})
        assert locked.status_code == 429
        # Even the right password is refused while locked.
        denied = await client.post(
            "/login", data={"username": "panel-admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert denied.status_code == 429


@pytest.mark.asyncio
async def test_session_cookie_flags(session: AsyncSession, monkeypatch, _clean_login_guard):
    """Cookie is HttpOnly + SameSite=Lax, and Secure follows the TLS setting."""
    from app.config import settings

    await _user(session, "panel-admin")
    async with _client() as client:
        plain = await client.post(
            "/login", data={"username": "panel-admin", "password": "secret123"},
            follow_redirects=False,
        )
        jar = plain.headers.get("set-cookie", "")
        assert "HttpOnly" in jar
        assert "SameSite=Lax" in jar or "samesite=lax" in jar.lower()
        assert "Secure" not in jar
    from app.panel.auth import clear_login_failures

    for _ip in ("127.0.0.1", "testclient", "unknown"):
        clear_login_failures(_ip)
    monkeypatch.setattr(settings, "panel_cookie_secure", True)
    async with _client() as client:
        tls = await client.post(
            "/login", data={"username": "panel-admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert "Secure" in tls.headers.get("set-cookie", "")
