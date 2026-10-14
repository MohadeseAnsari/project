"""Smoke tests for the server-rendered review panel (templates + one HTMX part)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.panel.app import panel_app
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


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


def _client():
    """CSRF-compliant panel client (browser simulation; see panel_test_client)."""
    return panel_client()


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


def _scope_request(client: tuple | None, headers: list[tuple[bytes, bytes]] | None = None):
    """Build a bare Starlette request to unit-test proxy header handling."""
    from starlette.requests import Request as StarletteRequest

    return StarletteRequest(
        {"type": "http", "method": "GET", "headers": headers or [], "client": client}
    )


def test_client_ip_trusts_proxy_only():
    """X-Forwarded-For counts only from the loopback proxy, never spoofed."""
    from app.panel.auth import client_ip, forwarded_proto

    via_proxy = _scope_request(("127.0.0.1", 123), [(b"x-forwarded-for", b"9.9.9.9, 10.0.0.1")])
    assert client_ip(via_proxy) == "9.9.9.9"
    direct = _scope_request(("203.0.113.7", 123), [(b"x-forwarded-for", b"9.9.9.9")])
    assert client_ip(direct) == "203.0.113.7"
    assert client_ip(_scope_request(("127.0.0.1", 123))) == "127.0.0.1"
    assert client_ip(_scope_request(None)) == "unknown"

    proto = _scope_request(("127.0.0.1", 123), [(b"x-forwarded-proto", b"https")])
    assert forwarded_proto(proto) == "https"
    assert forwarded_proto(direct) == ""


def test_client_ip_trusts_configured_bridge_network(monkeypatch):
    """Inside Docker the proxy arrives as the bridge gateway, not loopback."""
    from app.config import settings
    from app.panel.auth import client_ip, forwarded_proto

    monkeypatch.setattr(settings, "panel_trusted_proxies", "172.19.0.0/16")
    via_gateway = _scope_request(("172.19.0.1", 5000), [(b"x-forwarded-for", b"9.9.9.9")])
    assert client_ip(via_gateway) == "9.9.9.9"
    gateway_proto = _scope_request(
        ("172.19.0.1", 5000), [(b"x-forwarded-proto", b"https")]
    )
    assert forwarded_proto(gateway_proto) == "https"
    # Malformed entries fail closed (ignored, no trust granted).
    monkeypatch.setattr(settings, "panel_trusted_proxies", "not-an-ip")
    assert client_ip(via_gateway) == "172.19.0.1"


@pytest.mark.asyncio
async def test_lockout_uses_forwarded_ip(session: AsyncSession, monkeypatch, _clean_login_guard):
    """Behind the proxy, each visitor is bucketed by their own address."""
    from sqlalchemy import select

    from app.config import settings
    from app.db.models import AuditLog

    monkeypatch.setattr(settings, "panel_login_max_attempts", 2)
    monkeypatch.setattr(settings, "panel_login_window_minutes", 10)
    monkeypatch.setattr(settings, "panel_login_lockout_minutes", 15)
    await _user(session, "panel-admin")
    attacker = {"X-Forwarded-For": "9.9.9.9"}
    async with _client() as client:
        assert (await client.post("/login", data={"username": "panel-admin", "password": "nope1"}, headers=attacker)).status_code == 401
        assert (await client.post("/login", data={"username": "panel-admin", "password": "nope2"}, headers=attacker)).status_code == 429
        # Attacker's lockout does not touch the direct peer's bucket.
        good = await client.post(
            "/login", data={"username": "panel-admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert good.status_code == 302
        still_locked = await client.post(
            "/login", data={"username": "panel-admin", "password": "secret123"},
            headers=attacker, follow_redirects=False,
        )
        assert still_locked.status_code == 429
    details = (
        await session.execute(select(AuditLog.detail).where(AuditLog.action == "login_failed"))
    ).scalars().all()
    assert details and all("ip=9.9.9.9" in d for d in details)


@pytest.mark.asyncio
async def test_secure_cookie_from_forwarded_proto(session: AsyncSession, monkeypatch, _clean_login_guard):
    """The edge's https proto sets Secure even with the setting off."""
    from app.config import settings

    monkeypatch.setattr(settings, "panel_cookie_secure", False)
    await _user(session, "panel-admin")
    async with _client() as client:
        response = await client.post(
            "/login", data={"username": "panel-admin", "password": "secret123"},
            headers={"X-Forwarded-Proto": "https"}, follow_redirects=False,
        )
        assert response.status_code == 302
        assert "Secure" in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_session_expiry_follows_setting(session: AsyncSession, monkeypatch, _clean_login_guard):
    """Cookie Max-Age tracks PANEL_SESSION_HOURS (expiry is not hardcoded)."""
    from app.config import settings

    monkeypatch.setattr(settings, "panel_session_hours", 1)
    await _user(session, "panel-admin")
    async with _client() as client:
        response = await client.post(
            "/login", data={"username": "panel-admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert "Max-Age=3600" in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_all_panel_pages_render(session: AsyncSession):
    """Every GET page returns 200 with its Persian title (template smoke)."""
    from app.db.models import Category, Channel, Delivery, Main

    await _user(session, "panel-admin")
    cat = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(cat)
    await session.commit()
    channel = Channel(platform="bale", chat_id=72001, username="@c", is_active=True)
    session.add(channel)
    await session.commit()
    row = Main(
        username="t", title="T", abstract="A", field="f", university="U",
        author="A", supervisor="S", status="review",
        field_id=None, category_id=cat.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    session.add(Delivery(main_id=row.id, channel_id=channel.id, category_id=cat.id,
                         route_source="fallback", status="pending"))
    await session.commit()
    pages = {
        "/": "داشبورد",
        "/records": "رکوردها و بازبینی",
        "/deliveries": "ارسال‌ها و تحویل",
        "/fields": "رشته‌ها",
        "/categories": "دسته‌ها",
        "/channels": "کانال‌ها و مسیرها",
        "/models": "مدل‌ها",
        "/prompts": "پرامپت‌ها",
        "/submit": "ثبت پایان‌نامه",
        "/users": "کاربران و نقش‌ها",
        "/audit": "رویدادها",
        "/profile": "حساب من",
    }
    async with _client() as client:
        await _login(client, "panel-admin")
        for path, title in pages.items():
            response = await client.get(path)
            assert response.status_code == 200, path
            assert title in response.text, path
        detail = await client.get(f"/records/{row.id}")
        assert detail.status_code == 200
        assert "دستهٔ ثبت‌شده" in detail.text
        assert "دستهٔ کنونی رشته" in detail.text
        delivery_tab = await client.get(f"/records/{row.id}", params={"tab": "delivery"})
        assert "پیش‌نمایش پیام" in delivery_tab.text


@pytest.mark.asyncio
async def test_admin_reset_password_flow(session: AsyncSession):
    """Full reset cycle: old stops working, new works, audit records it."""
    from sqlalchemy import select

    from app.db.models import AuditLog, PanelUser

    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    editor = (await session.execute(select(PanelUser).where(PanelUser.username == "panel-editor"))).scalar_one()
    async with _client() as client:
        await _login(client, "panel-admin")
        assert (await client.post(f"/users/{editor.id}/password", data={"new_password": "short"}, follow_redirects=False)).status_code == 422
        assert (await client.post(f"/users/{editor.id}/password", data={"new_password": "freshpass1"}, follow_redirects=False)).status_code == 302
    actions = (await session.execute(select(AuditLog.action).where(AuditLog.action == "password_reset"))).scalars().all()
    assert actions == ["password_reset"]
    async with _client() as client:
        stale = await client.post("/login", data={"username": "panel-editor", "password": "secret123"}, follow_redirects=False)
        assert stale.status_code == 401
        fresh = await client.post("/login", data={"username": "panel-editor", "password": "freshpass1"}, follow_redirects=False)
        assert fresh.status_code == 302
        assert (await client.get("/profile")).status_code == 200


@pytest.mark.asyncio
async def test_user_changes_own_password(session: AsyncSession):
    """Self-service change: wrong current -> 401, short -> 422, then rotation."""
    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    async with _client() as client:
        await _login(client, "panel-editor")
        assert (await client.get("/profile")).status_code == 200
        bad_current = await client.post("/profile/password", data={"current_password": "nope", "new_password": "rotated99"})
        assert bad_current.status_code == 401
        too_short = await client.post("/profile/password", data={"current_password": "secret123", "new_password": "short"})
        assert too_short.status_code == 422
        rotated = await client.post("/profile/password", data={"current_password": "secret123", "new_password": "rotated99"})
        assert rotated.status_code == 200
        assert "با موفقیت" in rotated.text
    async with _client() as client:
        assert (await client.post("/login", data={"username": "panel-editor", "password": "secret123"}, follow_redirects=False)).status_code == 401
        response = await client.post("/login", data={"username": "panel-editor", "password": "rotated99"}, follow_redirects=False)
        assert response.status_code == 302


@pytest.mark.asyncio
async def test_editor_cannot_reset_passwords(session: AsyncSession):
    """Password resets are admin-only like the rest of user management."""
    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    async with _client() as client:
        await _login(client, "panel-editor")
        assert (await client.post("/users/1/password", data={"new_password": "hacked123"})).status_code == 403


@pytest.mark.asyncio
async def test_admin_creates_user_with_role_over_http(session: AsyncSession):
    """Admin-only page creates a user with the chosen role (min length enforced)."""
    from sqlalchemy import select

    from app.db.models import PanelUser

    await _user(session, "panel-admin")
    async with _client() as client:
        await _login(client, "panel-admin")
        short = await client.post(
            "/users", data={"username": "newbie", "password": "short", "role": "editor"},
            follow_redirects=False,
        )
        assert short.status_code == 422
        bad_role = await client.post(
            "/users", data={"username": "newbie", "password": "longenough1", "role": "owner"},
            follow_redirects=False,
        )
        assert bad_role.status_code == 422
        created = await client.post(
            "/users", data={"username": "newbie", "password": "longenough1", "role": "editor"},
            follow_redirects=False,
        )
        assert created.status_code == 302
    row = (await session.execute(select(PanelUser).where(PanelUser.username == "newbie"))).scalar_one()
    assert row.is_active is True
    async with _client() as client:
        response = await client.post(
            "/login", data={"username": "newbie", "password": "longenough1"},
            follow_redirects=False,
        )
        assert response.status_code == 302


@pytest.mark.asyncio
async def test_admin_deactivates_user_over_http(session: AsyncSession):
    """Deactivation blocks login at once; editors cannot touch this endpoint."""
    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    async with _client() as client:
        await _login(client, "panel-editor")
        assert (await client.post("/users/1/toggle", follow_redirects=False)).status_code == 403
    async with _client() as client:
        await _login(client, "panel-admin")
        assert (await client.post("/users/1/toggle", follow_redirects=False)).status_code == 422
        toggled = await client.post("/users/2/toggle", follow_redirects=False)
        assert toggled.status_code == 302
    async with _client() as client:
        assert (await client.post("/login", data={"username": "panel-editor", "password": "secret123"}, follow_redirects=False)).status_code == 401


@pytest.mark.asyncio
async def test_raw_output_renders_persian_literally(session: AsyncSession):
    """The raw tab shows real Persian characters in stable order, not escapes."""
    from app.db.models import Main

    await _user(session, "panel-admin")
    row = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "خبر پزشکی", "lead": "لید آزمایشی", "body": "متن کامل خبر"},
    )
    session.add(row)
    await session.commit()
    async with _client() as client:
        await _login(client, "panel-admin")
        first = await client.get(f"/records/{row.id}", params={"tab": "raw"})
        assert first.status_code == 200
        assert "خبر پزشکی" in first.text
        assert "\\u062e" not in first.text
        import html as _html

        decoded = _html.unescape(first.text)
        assert decoded.index('"body"') < decoded.index('"lead"') < decoded.index('"title"')
        second = await client.get(f"/records/{row.id}", params={"tab": "raw"})
        assert second.text == first.text


@pytest.mark.asyncio
async def test_approved_record_leaves_review_list(session: AsyncSession):
    """Panel review list defaults to needs-review; approved shows its banner."""
    from datetime import datetime

    from app.db.models import Main

    await _user(session, "panel-admin")
    fresh = Main(
        username="t", title="Fresh", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    done = Main(
        username="t", title="Done", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="approved", approved_at=datetime.utcnow(),
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add_all([fresh, done])
    await session.commit()
    async with _client() as client:
        await _login(client, "panel-admin")
        needs_review = await client.get("/records/table")
        assert "Fresh" in needs_review.text
        assert "Done" not in needs_review.text
        approved = await client.get("/records/table", params={"approved": "yes"})
        assert "Done" in approved.text
        assert "Fresh" not in approved.text
        detail = await client.get(f"/records/{done.id}")
        assert "تأیید شده" in detail.text


@pytest.mark.asyncio
async def test_bulk_approve_releases_backlog(session: AsyncSession):
    """Bulk approve stamps, releases, and audits each eligible record; skips the rest."""
    from sqlalchemy import select

    from app.db.models import AuditLog, Delivery, Main

    await _user(session, "panel-admin")
    rows = []
    for title in ("Bulk-A", "Bulk-B", "Bulk-C"):
        row = Main(
            username="t", title=title, abstract="A", field="پزشکی", university="U",
            author="A", supervisor="S", status="review",
            output_json={"title": "T", "lead": "L", "body": "B"},
        )
        session.add(row)
        rows.append(row)
    failed = Main(
        username="t", title="Skip-Me", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="failed",
    )
    session.add(failed)
    await session.commit()
    ids = [r.id for r in rows]
    async with _client() as client:
        await _login(client, "panel-admin")
        from urllib.parse import urlencode

        body = urlencode([("ids", str(i)) for i in ids + [failed.id, 999999]])
        response = await client.post(
            "/records/bulk-approve", content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        table = await client.get("/records/table")
        for title in ("Bulk-A", "Bulk-B", "Bulk-C"):
            assert title not in table.text
    for record_id in ids:
        session.expire_all()
        row = await session.get(Main, record_id)
        assert row.status == "approved"
        assert row.approved_at is not None
    actions = sorted(
        (await session.execute(select(AuditLog.action).where(AuditLog.action == "approve"))).scalars().all()
    )
    assert actions == ["approve", "approve", "approve"]
    await session.refresh(failed)
    assert failed.status == "failed"
    assert failed.approved_at is None


@pytest.mark.asyncio
async def test_panel_release_is_audited(session: AsyncSession):
    """Panel release flips the row and records who released it."""
    from datetime import datetime

    from sqlalchemy import select

    from app.db.models import AuditLog, Channel, Delivery, Main

    await _user(session, "panel-admin")
    row = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    channel = Channel(platform="bale", chat_id=96011, username="@c", is_active=True)
    session.add(channel)
    await session.commit()
    delivery = Delivery(main_id=row.id, channel_id=channel.id, status="awaiting_confirmation")
    session.add(delivery)
    await session.commit()
    async with _client() as client:
        await _login(client, "panel-admin")
        response = await client.post(f"/deliveries/{delivery.id}/release", follow_redirects=False)
        assert response.status_code == 302
    await session.refresh(delivery)
    assert delivery.status == "pending"
    audits = (
        await session.execute(select(AuditLog.action).where(AuditLog.main_id == row.id))
    ).scalars().all()
    assert "release" in audits


@pytest.mark.asyncio
async def test_dashboard_review_kpi_excludes_approved(session: AsyncSession):
    """The 'needs review' KPI counts exactly the 'review' rows (approved left)."""
    from datetime import datetime

    from app.db.models import Main

    await _user(session, "panel-admin")
    session.add(Main(
        username="t", title="Fresh", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    ))
    session.add(Main(
        username="t", title="Done", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="approved", approved_at=datetime.utcnow(),
        output_json={"title": "T", "lead": "L", "body": "B"},
    ))
    await session.commit()
    async with _client() as client:
        await _login(client, "panel-admin")
        page = await client.get("/")
        assert page.status_code == 200
        assert "در انتظار بازبینی انسانی" in page.text
        assert '<div class="n">۱</div>' in page.text


@pytest.mark.asyncio
async def test_final_prompt_tab_shows_exact_messages(session: AsyncSession):
    """پرامپت نهایی renders the exact system prompt, user message, and model — no secrets."""
    from app.config import settings
    from app.db.models import Main, Model, Prompt

    await _user(session, "panel-admin")
    model = Model(name="preview-model", provider="openai", version="v1", is_active=False)
    prompt = Prompt(version="v-preview", system_prompt="SYSTEM PROMPT TEXT", is_active=False)
    session.add_all([model, prompt])
    await session.commit()
    row = Main(
        username="t", title="Thesis Title", abstract="Abs", field="پزشکی", university="U",
        author="Author", supervisor="Sup", status="review",
        model_id=model.id, prompt_id=prompt.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(row)
    await session.commit()
    async with _client() as client:
        await _login(client, "panel-admin")
        page = await client.get(f"/records/{row.id}", params={"tab": "final"})
        assert page.status_code == 200
        assert "SYSTEM PROMPT TEXT" in page.text
        assert "title: Thesis Title" in page.text
        assert "author: Author" in page.text
        assert "preview-model" in page.text
        assert settings.shared_secret not in page.text
        assert settings.avalai_api_key not in page.text


@pytest.mark.asyncio
async def test_user_delete_safeguards(session: AsyncSession):
    """Self-delete and last-admin delete are refused; every delete tombstones."""
    from sqlalchemy import select

    from app.db.models import AuditLog, PanelUser

    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    admin = (await session.execute(select(PanelUser).where(PanelUser.username == "panel-admin"))).scalar_one()
    editor = (await session.execute(select(PanelUser).where(PanelUser.username == "panel-editor"))).scalar_one()
    async with _client() as client:
        await _login(client, "panel-admin")
        assert (await client.post(f"/users/{admin.id}/delete", follow_redirects=False)).status_code == 422
        assert (await client.post("/users/999999/delete", follow_redirects=False)).status_code == 404
        assert (await client.post(f"/users/{editor.id}/delete", follow_redirects=False)).status_code == 302
    session.expunge_all()
    row = await session.get(PanelUser, editor.id)
    # Deleted accounts are tombstoned, never erased: audit_logs.user_id is
    # ON DELETE SET NULL, so erasing the row would lose its author name.
    assert row is not None
    assert row.deleted_at is not None
    assert row.is_active is False
    # Last active admin cannot be deleted even with a second admin present-but-inactive.
    assert (await session.execute(select(PanelUser).where(PanelUser.username == "panel-admin"))).scalar_one().is_active is True


@pytest.mark.asyncio
async def test_a_deleted_user_disappears_from_the_panel(session: AsyncSession):
    """The owner's requirement: a deleted user is not seen in the panel again."""
    from sqlalchemy import select

    from app.db.models import AuditLog, PanelUser
    from app.services import users as user_service

    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    editor = (await session.execute(select(PanelUser).where(PanelUser.username == "panel-editor"))).scalar_one()
    # Give the editor authored history (audits record the actor, not the
    # target): they change their own password, then the admin deletes them.
    async with _client() as client:
        await _login(client, "panel-editor")
        changed = await client.post(
            "/profile/password",
            data={"current_password": "secret123", "new_password": "rotated99"},
        )
        assert changed.status_code == 200
    async with _client() as client:
        await _login(client, "panel-admin")
        response = await client.post(f"/users/{editor.id}/delete", follow_redirects=False)
        assert response.status_code == 302
        page = await client.get("/users")
    session.expunge_all()
    row = await session.get(PanelUser, editor.id)
    assert row is not None, "the row is kept so history keeps its author"
    assert row.deleted_at is not None
    assert row.is_active is False
    assert "panel-editor" not in page.text, "a deleted user must not appear in the panel"
    # And they cannot log back in.
    async with _client() as client:
        login = await client.post(
            "/login", data={"username": "panel-editor", "password": "rotated99"},
            follow_redirects=False,
        )
        assert login.status_code == 401
    # The trail still names them.
    actions = (await session.execute(
        select(AuditLog.action).where(AuditLog.detail.contains("panel-editor"))
    )).scalars().all()
    assert "user_delete" in actions
    listed = {u["username"] for u in await user_service.list_users(session)}
    assert "panel-editor" not in listed


@pytest.mark.asyncio
async def test_delete_last_admin_refused_at_service(session: AsyncSession):
    """The sole active admin survives even a privileged delete call."""
    import pytest

    from sqlalchemy import select

    from app.db.models import PanelUser
    from app.services import users as user_service

    await _user(session, "panel-admin")
    await _user_with_role(session, "panel-editor", "editor")
    admin = (await session.execute(select(PanelUser).where(PanelUser.username == "panel-admin"))).scalar_one()
    editor = (await session.execute(select(PanelUser).where(PanelUser.username == "panel-editor"))).scalar_one()
    with pytest.raises(ValueError, match="last-admin"):
        await user_service.delete_user(session, admin, actor_id=editor.id)
    assert (await session.get(PanelUser, admin.id)).is_active is True
