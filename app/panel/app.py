"""Server-rendered review panel (Jinja2 + locally vendored HTMX, no build step).

Same image as the API with a different command (``web`` compose service).
Persian RTL UI, plain CSS, Vazirmatn fonts vendored under ``/static``.
Human auth uses panel users + signed cookies (not the machine shared secret);
every review/retry/config change is written to ``audit_logs`` for history.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import jdatetime
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.panel.auth import ensure_admin_user, session_user_id, verify_password
from app.panel.csrf import CSRFError, csrf_field, verify_csrf
from app.services import audit as audit_service
from app.services import channels as channel_service
from app.services import deliveries as delivery_service
from app.services import dispose_engine
from app.services import fields as field_service
from app.services import models as model_service
from app.services import prompts as prompt_service
from app.services import records as record_service
from app.services import routing as routing_service
from app.services import session_dep as service_session_dep
from app.services import users as user_service
from app.services.transitions import STATUS_FA

_here = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_here / "templates"))


def _jalali(value) -> str:
    """Format a gregorian datetime as a Jalali string for Persian readers."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        try:
            return jdatetime.datetime.fromgregorian(datetime=value).strftime("%Y/%m/%d %H:%M")
        except Exception:
            return str(value)
    return str(value)


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _fa(value) -> str:
    """Render numbers with Persian digits to match the approved design."""
    if value is None:
        return "—"
    return str(value).translate(_FA_DIGITS)


_ROLE_FA = {"admin": "ادمین", "editor": "بازبین", "viewer": "بیننده"}


def _role_fa(user) -> str:
    """Persian display name for a panel role (design reference wording)."""
    if user is None or not getattr(user, "role", None):
        return "—"
    return _ROLE_FA.get(user.role.name, user.role.name)


def _tojson_fa(value) -> str:
    """Render JSON with real Persian characters in stable key order.

    Jinja's ``tojson`` escapes non-ASCII as ``\\uXXXX``, which reviewers
    cannot read or copy-paste; ``ensure_ascii=False`` keeps Persian text
    literal and ``sort_keys`` keeps repeated renders byte-identical.
    """
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


templates.env.filters["jalali"] = _jalali
templates.env.filters["fa"] = _fa
templates.env.filters["tojson_fa"] = _tojson_fa
templates.env.globals["role_fa"] = _role_fa
templates.env.globals["csrf_field"] = csrf_field


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the first admin from env, dispose the shared engine on shutdown."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await ensure_admin_user()
    yield
    await dispose_engine()


#: One DB session per request, provided by the service layer: the panel never
#: opens a session or imports an ORM model itself (``tests/test_panel_layering.py``).
session_dep = service_session_dep


panel_app = FastAPI(
    title="IranDoc Review Panel", docs_url=None, redoc_url=None, lifespan=lifespan,
    dependencies=[Depends(verify_csrf)],
)
panel_app.mount("/static", StaticFiles(directory=str(_here / "static")), name="static")


@panel_app.exception_handler(CSRFError)
async def _csrf_error_handler(request: Request, exc: CSRFError):
    """Render CSRF failures as a Persian 400 page (never a stack trace)."""
    return HTMLResponse(
        f'<meta charset="utf-8"><div class="note">{exc.message}</div>',
        status_code=400,
    )


async def current_user(request: Request, session: AsyncSession) -> object | None:
    """Load the logged-in panel user (with role) or None, via the users service."""
    uid = session_user_id(request)
    if uid is None:
        return None
    return await user_service.get_active_user_with_role(session, uid)


async def require_login(request: Request, session: AsyncSession):
    """Redirect anonymous browsers to the login page (panel is human-only)."""
    user = await current_user(request, session)
    if user is None:
        raise _LoginRedirect()
    return user


class _LoginRedirect(Exception):
    """Internal control flow: unauthenticated panel access goes to /login."""


@panel_app.exception_handler(_LoginRedirect)
async def _login_redirect_handler(request: Request, exc: _LoginRedirect):
    return RedirectResponse(url="/login", status_code=302)


def _ctx(request: Request, user: object | None, **extra):
    base = {
        "request": request, "current_user": user, "active": "",
        "error": None, "ok": None, "review_count": None, "gated_count": None,
    }
    base.update(extra)
    return base


async def _sidebar_counts(session: AsyncSession) -> dict:
    """Sidebar badges: records awaiting review + deliveries needing attention."""
    review_total = await record_service.needs_review_count(session)
    counts = await delivery_service.delivery_status_counts(session)
    gated_total = counts.get("failed", 0) + counts.get("awaiting_confirmation", 0)
    return {"review_count": review_total or None, "gated_count": gated_total or None}


def _is_admin(user) -> bool:
    return bool(user.role and user.role.name == "admin")


def _can_review(user) -> bool:
    """Review rights: admin full access, editor/reviewer may approve/reject/retry/submit.

    Viewers are read-only — every mutation returns 403 for them.
    """
    return bool(user.role and user.role.name in ("admin", "editor"))


# --------------------------------------------------------------------------
# auth pages
# --------------------------------------------------------------------------


@panel_app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, session: AsyncSession = Depends(session_dep)):
    """Show the Persian login form (public; the only anonymous page)."""
    user = await current_user(request, session)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html", _ctx(request, None))


@panel_app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(session_dep),
):
    """Verify credentials, sign the session cookie, and audit the login.

    Public-exposure hardening: per-IP lockout is checked before any password
    work; failures are counted and written to the audit trail, and reaching
    the cap locks the IP (429). Passwords are verified with argon2, which
    compares in a way that does not bail out early on the first wrong byte.
    """
    from app.panel.auth import (
        clear_login_failures,
        client_ip,
        forwarded_proto,
        is_login_locked,
        record_login_failure,
        set_session_cookie,
    )

    ip = client_ip(request)
    if is_login_locked(ip):
        return templates.TemplateResponse(request, "login.html",
            _ctx(request, None, error="تلاش‌های ناموفق زیاد بود؛ لطفاً چند دقیقه بعد دوباره تلاش کنید."),
            status_code=429)

    row = await user_service.get_user_by_username(session, username)
    if not row or not row.is_active or not verify_password(row.password_hash, password):
        await audit_service.record_event(
            session, row.id if row else None, "login_failed", None, f"username={username} ip={ip}"
        )
        just_locked = record_login_failure(ip)
        if just_locked:
            return templates.TemplateResponse(request, "login.html",
                _ctx(request, None, error="تلاش‌های ناموفق زیاد بود؛ لطفاً چند دقیقه بعد دوباره تلاش کنید."),
                status_code=429)
        return templates.TemplateResponse(
            request, "login.html",
            _ctx(request, None, error="نام کاربری یا گذرواژه نادرست است."), status_code=401,
        )
    clear_login_failures(ip)
    await audit_service.record_event(session, row.id, "login", None, f"username={username} ip={ip}")
    response = RedirectResponse(url="/", status_code=302)
    # HTTPS is guaranteed at the edge: Secure when configured or when the
    # trusted proxy reports https (direct plain-HTTP logins keep working).
    set_session_cookie(
        response, row.id,
        secure=bool(settings.panel_cookie_secure or forwarded_proto(request) == "https"),
    )
    return response


@panel_app.get("/logout")
async def logout():
    """Clear the session cookie and return to the login page."""
    from app.panel.auth import clear_session_cookie

    response = RedirectResponse(url="/login", status_code=302)
    clear_session_cookie(response)
    return response


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@panel_app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(session_dep)):
    """Show KPIs, a 7-day chart, health, recent events, and channel state."""
    from datetime import timedelta

    user = await require_login(request, session)

    counts = await record_service.record_status_counts(session)
    delivery_counts = await delivery_service.delivery_status_counts(session)
    kpi_review = await record_service.needs_review_count(session)
    kpi_queued = counts.get("ready", 0) + counts.get("processing", 0)
    kpi_sent = delivery_counts.get("sent", 0)
    kpi_failed = counts.get("failed", 0) + delivery_counts.get("failed", 0)

    # Real 7-day series: records created per day, heights normalized.
    today = datetime.utcnow().date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = await record_service.records_per_day(session, 7)
    peak = max((n for _, n in per_day), default=0)
    chart = [
        {
            "label": jdatetime.date.fromgregorian(date=d).strftime("%A"),
            "pct": int(round(n / peak * 100)) if peak else 0,
        }
        for d, n in per_day
    ]

    heartbeat = await record_service.worker_heartbeat(session)
    worker_fresh = bool(
        heartbeat and (datetime.utcnow() - heartbeat).total_seconds() < settings.worker_lease_minutes * 60
    )
    pending_deliveries = delivery_counts.get("pending", 0)

    # Timeline: latest human/system events from the audit trail.
    timeline = []
    for entry in await audit_service.recent_events(session, 4):
        title = _audit_title(entry)
        timeline.append(
            {
                "icon": {"approve": "✓", "reject": "!", "retry": "➤", "login": "◕"}.get(entry["action"], "•"),
                "color": {"approve": "green", "reject": "red", "retry": "amber", "login": "violet"}.get(entry["action"], "blue"),
                "title": title,
                "sub": f"{entry['username'] or 'سیستم'} · {_jalali(entry['created_at'])}",
            }
        )

    channels = await channel_service.channels_with_route_counts(session)

    failed_note = None
    if delivery_counts.get("failed", 0):
        failed_note = f"{_fa(delivery_counts['failed'])} تحویل ناموفق در صف اقدام دستی است؛ از صفحهٔ ارسال‌ها «تلاش مجدد» را بزنید."

    counts_ctx = await _sidebar_counts(session)
    now_label = jdatetime.datetime.now().strftime("%H:%M")
    return templates.TemplateResponse(request, "dashboard.html",
        _ctx(
            request, user, active="dashboard", **counts_ctx,
            kpi_review=kpi_review, kpi_queued=kpi_queued, kpi_sent=kpi_sent, kpi_failed=kpi_failed,
            chart=chart, worker_fresh=worker_fresh, pending_deliveries=pending_deliveries,
            timeline=timeline, channels=channels, failed_note=failed_note, now_label=now_label,
        ),
    )


_ACTION_FA = {
    "login": "ورود به کنسول", "login_failed": "ورود ناموفق", "approve": "تأیید و انتشار", "reject": "رد خبر",
    "save_draft": "ذخیرهٔ پیش‌نویس", "retry": "تلاش مجدد ارسال", "release": "آزادسازی ارسال",
    "submit": "ثبت رکورد", "model_create": "ساخت مدل", "model_activate": "فعال‌سازی مدل",
    "curate": "تأیید انسانی دسته",
    "model_test": "آزمون اتصال مدل", "prompt_create": "ساخت پرامپت",
    "prompt_activate": "فعال‌سازی پرامپت", "channel_create": "ساخت کانال",
    "route": "افزودن مسیر", "unroute": "حذف مسیر",
    "user_create": "ساخت کاربر", "user_role": "تغییر نقش کاربر", "user_toggle": "تغییر وضعیت کاربر",
    "user_delete": "حذف کاربر",
    "password_reset": "ثبت گذرواژهٔ کاربر", "password_change": "تغییر گذرواژهٔ خود",
    "regenerate": "تولید دوباره با مدل",
}


def _audit_title(entry) -> str:
    """Persian one-line summary of an audit entry for timelines and tables."""
    if isinstance(entry, dict):
        action = entry.get("action")
        main_id = entry.get("main_id")
        detail = entry.get("detail")
    else:  # ORM AuditLog (record history path)
        action, main_id, detail = entry.action, entry.main_id, entry.detail
    label = _ACTION_FA.get(action, action)
    if main_id:
        return f"{label} — رکورد {main_id}"
    if detail:
        return f"{label} — {detail}"
    return label


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@panel_app.get("/records", response_class=HTMLResponse)
async def records_page(
    request: Request, session: AsyncSession = Depends(session_dep),
    status: str = "", field: str = "", university: str = "",
    q: str = "", order: str = "newest", page: int = 1, approved: str = "no",
):
    """Show the filterable record list (first page server-rendered, rest via HTMX)."""
    user = await require_login(request, session)
    from urllib.parse import urlencode

    page = max(1, page)
    page_size = 20
    counts = await record_service.record_status_counts(session)
    delivery_counts = await delivery_service.delivery_status_counts(session)
    options = await record_service.filter_options(session)
    approved_flag = {"yes": True, "no": False}.get(approved)
    items, total = await record_service.list_records(
        session, page=page, page_size=page_size, status=status or None,
        field=field or None, university=university or None, q=q or None,
        order=order, approved=approved_flag,
    )
    needs_review = await record_service.needs_review_count(session)
    counts_ctx = await _sidebar_counts(session)
    query = urlencode({k: v for k, v in {"status": status, "field": field, "university": university, "q": q, "order": order, "approved": approved}.items() if v})
    return templates.TemplateResponse(request, "records.html",
        _ctx(
            request, user, active="records", **counts_ctx,
            status=status, field=field, university=university, q=q, order=order, approved=approved, query=query,
            kpi_review=needs_review,
            kpi_queued=counts.get("ready", 0) + counts.get("processing", 0),
            kpi_sent=delivery_counts.get("sent", 0),
            kpi_failed=counts.get("failed", 0) + delivery_counts.get("failed", 0),
            field_options=options["fields"], university_options=options["universities"],
            items=items, total=total, page=page, page_size=page_size,
        ),
    )


@panel_app.get("/records/table", response_class=HTMLResponse)
async def records_table(
    request: Request, session: AsyncSession = Depends(session_dep),
    status: str = "", field: str = "", university: str = "",
    q: str = "", order: str = "newest", page: int = 1, page_size: int = 20,
    approved: str = "no",
):
    """HTMX partial: one page of filtered record rows (used by the list page)."""
    user = await require_login(request, session)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    approved_flag = {"yes": True, "no": False}.get(approved)
    items, total = await record_service.list_records(
        session, page=page, page_size=page_size, status=status or None,
        field=field or None, university=university or None, q=q or None,
        order=order, approved=approved_flag,
    )
    from urllib.parse import urlencode

    query_str = urlencode({k: v for k, v in {"status": status, "field": field, "university": university, "q": q, "order": order, "approved": approved}.items() if v})
    return templates.TemplateResponse(request, "records_table.html",
        {"request": request, "current_user": user, "items": items, "total": total, "page": page, "page_size": page_size, "query": query_str},
    )


class _DisplayDelivery:
    """Attribute-access view over service delivery dicts for templates."""

    def __init__(self, payload: dict):
        self.__dict__.update(payload)


@panel_app.get("/records/{record_id}", response_class=HTMLResponse)
async def record_detail(
    request: Request, record_id: int, session: AsyncSession = Depends(session_dep),
    tab: str = "text",
):
    """Show one record with editable output, raw/meta/history/delivery tabs."""
    user = await require_login(request, session)
    if tab not in ("text", "raw", "meta", "history", "delivery", "final"):
        tab = "text"
    record = await record_service.get_record(session, record_id)
    if not record:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رکورد یافت نشد."), status_code=404)
    overview = await record_service.record_overview(session, record)
    # Deliveries as display objects (template reads attributes).
    deliveries = [_DisplayDelivery(d) for d in overview["deliveries"]]
    # History: lifecycle steps from real timestamps, then audit entries.
    history = [
        {"title": "ثبت درخواست", "sub": f"وضعیت: ready · {_jalali(record.created_at)}"},
    ]
    if record.claimed_at:
        history.append({"title": "worker پردازش را برداشت", "sub": f"وضعیت: processing · {_jalali(record.claimed_at)}"})
    if record.output_json:
        history.append({"title": "خروجی مدل ذخیره شد", "sub": f"وضعیت: review · {_jalali(record.updated_at)}"})
    elif record.error_message:
        history.append({"title": "تولید ناموفق ماند", "sub": f"وضعیت: failed · {_jalali(record.updated_at)}"})
    if record.status == "review":
        history.append({"title": "در انتظار تصمیم بازبین", "sub": "پس از تأیید: ارسال به کانال‌های مسیرشده"})
    elif record.status == "approved":
        history.append({"title": "تأیید بازبین", "sub": f"در صف ارسال · {_jalali(record.approved_at)}" if record.approved_at else "در صف ارسال"})
    elif record.status == "published":
        history.append({"title": "منتشر شد", "sub": _jalali(record.published_at)})
    elif record.status == "rejected":
        history.append({"title": "خبر رد شد", "sub": f"{_jalali(record.updated_at)}"})
    for entry in overview["history"]:
        action = _ACTION_FA.get(entry["title"], entry["title"])
        history.append({"title": action, "sub": f"{entry['username'] or 'سیستم'} · {_jalali(entry['created_at'])}"})
    model_name = overview["model_name"] or settings.llm_model
    prompt_version = overview["prompt_version"]
    prompt_text = overview["prompt_text"]
    if prompt_text is None:
        from app.news.prompts import SYSTEM_PROMPT

        prompt_text = SYSTEM_PROMPT
    from app.news.generation import build_user_message, resolve_base_url

    prompt_preview = {
        "model_name": model_name,
        "base_url": overview["model_base_url"] or resolve_base_url(None),
        "system_prompt": prompt_text,
        "user_message": build_user_message({
            "title": record.title, "abstract": record.abstract, "field": record.field,
            "university": record.university, "table_of_contents": record.table_of_contents,
            "author": record.author, "supervisor": record.supervisor,
        }),
    }
    routed_channels = None
    message_preview = None
    preview_channel = None
    # Same route resolution the sender uses (no copy of the query here).
    from app.messaging.dispatcher import channels_for_article
    from app.messaging.formatting import render_article

    routed = await channels_for_article(session, record)
    if routed:
        routed_channels = "، ".join(c.username or c.title or c.platform for c in routed)
    if tab == "delivery" and record.output_json:
        # Show exactly what approval would publish (first routed channel).
        preview_channel = routed[0] if routed else None
        message_preview = render_article(
            record.output_json, preview_channel.username if preview_channel else None
        ) or None
    approved_by = None
    if record.approved_at is not None:
        info = await record_service.approval_info(session, record)
        if info is not None:
            approved_by = f"{info[0]} · {_jalali(info[1])}"
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "record_detail.html",
        _ctx(
            request, user, active="records", **counts_ctx, record=record, deliveries=deliveries,
            history=history, tab=tab, model_name=model_name, prompt_version=prompt_version,
            approved_by=approved_by,
            routed_channels=routed_channels, message_preview=message_preview,
            snapshot_category=overview["snapshot_category"],
            current_category=overview["current_category"],
            field_keyword=overview["field_keyword"], prompt_preview=prompt_preview,
            preview_channel=(preview_channel.username or preview_channel.title or preview_channel.platform) if preview_channel else None,
        )
    )


@panel_app.post("/records/{record_id}/regenerate")
async def record_regenerate(
    request: Request, record_id: int, session: AsyncSession = Depends(session_dep)
):
    """Re-run LLM generation for a ``review``/``failed`` record.

    All generation logic lives in ``record_service.regenerate`` (the exact
    same resolution + generation path as the Worker), so the panel never
    touches a record row directly and no route sets a status by hand.
    """
    user = await require_login(request, session)
    if not _can_review(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    try:
        record = await record_service.regenerate(session, record_id)
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="تولید دوباره فقط برای رکوردهای بررسی‌شده یا ناموفق ممکن است."), status_code=409)
    except Exception:
        # The service already stored the error on the record and marked it
        # failed; the audit row records the failed attempt.
        await audit_service.record_event(session, user.id, "regenerate", record_id, "تولید دوباره ناموفق ماند")
        return RedirectResponse(url=f"/records/{record_id}", status_code=302)
    await audit_service.record_event(session, user.id, "regenerate", record.id, "تولید دوباره با مدل")
    return RedirectResponse(url=f"/records/{record_id}", status_code=302)


@panel_app.post("/records/{record_id}/review")
async def record_review(
    request: Request, record_id: int, session: AsyncSession = Depends(session_dep),
    action: str = Form(...), title: str = Form(""), lead: str = Form(""), body: str = Form(""),
):
    """Apply approve/reject/save_draft from the review form and audit it."""
    user = await require_login(request, session)
    if not _can_review(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    if action not in ("approve", "reject", "save_draft"):
        return RedirectResponse(url=f"/records/{record_id}", status_code=302)
    record = await record_service.get_record(session, record_id)
    if record is None or record.status != "review":
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رکورد در وضعیت بررسی نیست."), status_code=409)
    try:
        await record_service.apply_review(
            session, record, action=action,
            title=title.strip() or None, lead=lead.strip() or None, body=body.strip() or None,
        )
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رکورد در وضعیت بررسی نیست."), status_code=409)
    await audit_service.record_event(
        session, user.id, action, record.id,
        {"approve": "تأیید و انتشار", "reject": "رد رکورد"}.get(action, "ذخیره پیش‌نویس"),
    )
    return RedirectResponse(url=f"/records/{record_id}", status_code=302)


@panel_app.post("/records/bulk-approve")
async def records_bulk_approve(request: Request, session: AsyncSession = Depends(session_dep)):
    """Approve many review records at once (backlog friendly).

    Only rows still in ``review`` transition; anything else is skipped, never
    forced. Each approval stamps ``approved_at`` (first wins), releases gated
    deliveries, and writes its own audit row — the same per-record semantics
    as the single approve, applied in one pass.
    """
    user = await require_login(request, session)
    if not _can_review(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    form = await request.form()
    ids = []
    for value in form.getlist("ids"):
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    ids = sorted(set(ids))

    approved, _ = await record_service.bulk_approve(session, ids)
    for record_id in approved:
        await audit_service.record_event(session, user.id, "approve", record_id, "تأیید گروهی")
    return RedirectResponse(url="/records", status_code=302)


# --------------------------------------------------------------------------
# deliveries
# --------------------------------------------------------------------------


@panel_app.get("/deliveries", response_class=HTMLResponse)
async def deliveries_page(
    request: Request, session: AsyncSession = Depends(session_dep),
    status: str = "", platform: str = "", main_id: str = "",
):
    """Show the filterable delivery list (first page server-rendered)."""
    user = await require_login(request, session)
    from urllib.parse import urlencode

    page_size = 20
    status_f = status or None
    platform_f = platform or None
    mid = int(main_id.strip()) if main_id.strip().isdigit() else None
    pairs, total = await delivery_service.fetch_deliveries(
        session, page=1, page_size=page_size, status=status_f,
        platform=platform_f, main_id=mid,
    )
    items = await delivery_service.delivery_items(session, pairs)
    newest_failure = await delivery_service.last_failed(session)
    last_error = (
        f"خطای آخر: «{newest_failure.error_message}» — {_jalali(newest_failure.updated_at)}"
        if newest_failure and newest_failure.error_message else None
    )
    delivery_counts = await delivery_service.delivery_status_counts(session)
    counts_ctx = await _sidebar_counts(session)
    query_str = urlencode({k: v for k, v in {"status": status, "platform": platform, "main_id": main_id}.items() if v})
    return templates.TemplateResponse(request, "deliveries.html",
        _ctx(
            request, user, active="deliveries", **counts_ctx, status=status, platform=platform,
            main_id=main_id, query=query_str, items=items, total=total, page=1, page_size=page_size,
            last_error=last_error, max_attempts=settings.sender_max_attempts,
            backoff_base=settings.sender_backoff_base_seconds,
            failed_count=delivery_counts.get("failed", 0),
        )
    )


@panel_app.get("/deliveries/table", response_class=HTMLResponse)
async def deliveries_table(
    request: Request, session: AsyncSession = Depends(session_dep),
    status: str = "", platform: str = "", main_id: str = "", page: int = 1, page_size: int = 20,
):
    """HTMX partial: one page of filtered deliveries (used by the list page)."""
    user = await require_login(request, session)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    status_f = status or None
    platform_f = platform or None
    mid = int(main_id.strip()) if main_id.strip().isdigit() else None
    pairs, total = await delivery_service.fetch_deliveries(
        session, page=page, page_size=page_size, status=status_f,
        platform=platform_f, main_id=mid,
    )
    items = await delivery_service.delivery_items(session, pairs)
    from urllib.parse import urlencode

    query_str = urlencode({k: v for k, v in {"status": status, "platform": platform, "main_id": main_id}.items() if v})
    return templates.TemplateResponse(request, "deliveries_table.html",
        {"request": request, "current_user": user, "items": items, "total": total, "page": page, "page_size": page_size, "query": query_str},
    )


@panel_app.post("/deliveries/{delivery_id}/retry")
async def delivery_retry(
    request: Request, delivery_id: int, session: AsyncSession = Depends(session_dep)
):
    """Re-queue a failed delivery (attempts reset) and audit the retry."""
    user = await require_login(request, session)
    if not _can_review(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    delivery = await delivery_service.get_delivery(session, delivery_id)
    if delivery is None:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال یافت نشد."), status_code=404)
    try:
        delivery = await delivery_service.retry_delivery(session, delivery)
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال قابل تلاش مجدد نیست."), status_code=409)
    await audit_service.record_event(session, user.id, "retry", delivery.main_id, f"delivery={delivery.id}")
    return RedirectResponse(url="/deliveries", status_code=302)


@panel_app.post("/deliveries/{delivery_id}/release")
async def delivery_release(
    request: Request, delivery_id: int, session: AsyncSession = Depends(session_dep)
):
    """Release one gated delivery for sending and audit who released it."""
    user = await require_login(request, session)
    if not _can_review(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    delivery = await delivery_service.get_delivery(session, delivery_id)
    if delivery is None:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال یافت نشد."), status_code=404)
    try:
        delivery = await delivery_service.release_delivery(session, delivery)
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="این ارسال در انتظار تأیید نیست."), status_code=409)
    await audit_service.record_event(session, user.id, "release", delivery.main_id, f"delivery={delivery.id}")
    return RedirectResponse(url="/deliveries", status_code=302)


# --------------------------------------------------------------------------
# reference data
# --------------------------------------------------------------------------


@panel_app.get("/fields", response_class=HTMLResponse)
async def fields_page(request: Request, session: AsyncSession = Depends(session_dep)):
    """List known thesis fields with categories, routes, and top-field bars."""
    user = await require_login(request, session)

    fields = await field_service.list_fields(session)
    total = await field_service.field_total(session)
    top = await field_service.top_fields(session)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "fields.html",
        _ctx(request, user, active="fields", **counts_ctx, fields=fields, total=total, top=top))


@panel_app.post("/fields/{field_id}/confirm")
async def field_confirm(request: Request, field_id: int, session: AsyncSession = Depends(session_dep)):
    """Freeze a field's category as human-curated (reviewers and admins)."""
    user = await require_login(request, session)
    if not _can_review(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)

    field = await field_service.get_field(session, field_id)
    if not field:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رشته یافت نشد."), status_code=404)
    field = await field_service.confirm_category(session, field)
    await audit_service.record_event(session, user.id, "curate", None, f"field={field.name}")
    return RedirectResponse(url="/fields", status_code=302)


@panel_app.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request, q: str = "", session: AsyncSession = Depends(session_dep)):
    """List categories as cards with their real field names as chips."""
    user = await require_login(request, session)

    categories, total = await field_service.category_cards(session, q)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "categories.html",
        _ctx(request, user, active="categories", **counts_ctx, categories=categories, total=total, q=q))


@panel_app.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request, channel_id: int | None = None, new: str = "", session: AsyncSession = Depends(session_dep)):
    """List channels with route matrix and the selected channel settings."""
    user = await require_login(request, session)

    channels = await channel_service.channels_with_route_counts(session)
    selected = None
    if channel_id is not None:
        selected = await channel_service.get_channel(session, channel_id)
    if selected is None:
        selected = channels[0] if channels else None
    matrix = await channel_service.field_route_matrix(session)
    _, cat_routes = await channel_service.category_route_matrix(session)
    categories = await field_service.list_categories(session)
    all_fields = await field_service.list_field_options(session)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "channels.html",
        _ctx(request, user, active="channels", **counts_ctx, channels=channels, selected=selected,
             matrix=matrix, cat_routes=cat_routes, categories=categories, all_fields=all_fields,
             show_new=bool(new)))



@panel_app.post("/channels/route")
async def channel_route(request: Request, field_id: int = Form(...), channel_id: int = Form(...), session: AsyncSession = Depends(session_dep)):
    """Route one field to one channel (admin only; row insert, no code change)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    await channel_service.add_field_route(session, field_id=field_id, channel_id=channel_id)
    await audit_service.record_event(session, user.id, "route", None, f"field={field_id} channel={channel_id}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.post("/channels/unroute")
async def channel_unroute(request: Request, field_id: int = Form(...), channel_id: int = Form(...), session: AsyncSession = Depends(session_dep)):
    """Remove one field→channel route (admin only)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    await channel_service.remove_field_route(session, field_id=field_id, channel_id=channel_id)
    await audit_service.record_event(session, user.id, "unroute", None, f"field={field_id} channel={channel_id}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.post("/channels/category-route")
async def channel_category_route(request: Request, category_id: int = Form(...), channel_id: int = Form(...), session: AsyncSession = Depends(session_dep)):
    """Route one category to one channel (admin only; the primary mapping)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    await channel_service.add_category_route(session, category_id=category_id, channel_id=channel_id)
    await audit_service.record_event(session, user.id, "route", None, f"category={category_id} channel={channel_id}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.post("/channels/category-unroute")
async def channel_category_unroute(request: Request, category_id: int = Form(...), channel_id: int = Form(...), session: AsyncSession = Depends(session_dep)):
    """Remove one category→channel route (admin only)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    await channel_service.remove_category_route(session, category_id=category_id, channel_id=channel_id)
    await audit_service.record_event(session, user.id, "unroute", None, f"category={category_id} channel={channel_id}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.get("/routing", response_class=HTMLResponse)
async def routing_page(request: Request, field: str = "", record_id: str = "", session: AsyncSession = Depends(session_dep)):
    """Show the auditable routing chain for a field name or a record."""
    user = await require_login(request, session)
    preview = None
    error = None
    if field.strip() or record_id.strip():
        try:
            preview = await routing_service.preview(
                session,
                field=field.strip() or None,
                record_id=int(record_id.strip()) if record_id.strip().isdigit() else None,
            )
        except ValueError:
            error = "فقط یکی از نام رشته یا شناسهٔ رکورد را وارد کنید."
            preview = None
        except LookupError:
            error = "رکورد یافت نشد."
            preview = None
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "routing.html",
        _ctx(request, user, active="routing", **counts_ctx, field=field, record_id=record_id,
             preview=preview, error=error))


@panel_app.post("/channels/{channel_id}/toggle")
async def channel_toggle(request: Request, channel_id: int, session: AsyncSession = Depends(session_dep)):
    """Flip a channel's active switch (admin only)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await channel_service.get_channel(session, channel_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کانال یافت نشد."), status_code=404)
    row = await channel_service.toggle_channel(session, row)
    await audit_service.record_event(session, user.id, "channel_create", None, f"toggle channel={channel_id} active={row.is_active}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.post("/channels/{channel_id}")
async def channel_update(
    request: Request, channel_id: int, title: str = Form(""),
    chat_id: str = Form(""), username: str = Form(""), bot_token_env: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Update a channel's display fields (admin only; no tokens on rows)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await channel_service.get_channel(session, channel_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کانال یافت نشد."), status_code=404)
    updates = {}
    if title is not None:
        updates["title"] = title.strip() or None
    if username is not None:
        updates["username"] = username.strip() or None
    if bot_token_env is not None:
        updates["bot_token_env"] = bot_token_env.strip() or None
    if chat_id.strip().lstrip("-").isdigit():
        updates["chat_id"] = int(chat_id.strip())
    row = await channel_service.update_channel(session, row, updates)
    await audit_service.record_event(session, user.id, "channel_create", None, f"update channel={channel_id}")
    return RedirectResponse(url=f"/channels?channel_id={channel_id}", status_code=302)


@panel_app.post("/channels")
async def channel_create(
    request: Request, platform: str = Form(...), target: str = Form(...),
    title: str = Form(""), bot_token_env: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Create a channel destination after live getChat validation (admin only).

    Accepts a numeric chat id or an @username; the platform API resolves it
    first, so typos fail here instead of silently parking every future send.
    Tokens stay in env (platform-wide for creation); the resolved title is
    shown on the channel card right after redirect.
    """
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    target = target.strip()
    try:

        row = await channel_service.create_channel(
            session, platform=platform, target=target,
            title=title.strip() or None,
            bot_token_env=bot_token_env.strip() or None,
        )
        await audit_service.record_event(session, user.id, "channel_create", None, f"{platform}:{row.chat_id}")
    except ValueError as e:
        message = {
            "bad-platform": "سکوی نامعتبر است.",
            "no-platform-token": f"توکن ربات {platform} پیکربندی نشده است.",
            "duplicate-channel": "این کانال قبلاً ثبت شده است.",
            "bad-result": "پاسخ getChat شناسهٔ معتبر نداشت.",
        }.get(str(e), f"کانال یافت نشد: {e}")
        status = 422
        if str(e).startswith("unresolvable:"):
            message = f"کانال یافت نشد: {str(e)[len('unresolvable: '):]}"
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=status)
    return RedirectResponse(url="/channels", status_code=302)




# --------------------------------------------------------------------------
# models & prompts (admin only for mutations; immutable prompt versions)
# --------------------------------------------------------------------------


def _attach_creds(rows) -> None:
    """Attach resolved credential summaries (never key values) for display."""
    from app.news.generation import describe_model_credentials

    for row in rows:
        row.creds = describe_model_credentials(row)


@panel_app.get("/models", response_class=HTMLResponse)
async def models_page(request: Request, model_id: int | None = None, session: AsyncSession = Depends(session_dep)):
    """List models with the selected (default active) settings and test cards."""
    user = await require_login(request, session)

    rows = await model_service.list_models(session)
    selected = None
    if model_id is not None:
        selected = await model_service.get_model(session, model_id)
    if selected is None:
        selected = next((m for m in rows if m.is_active), rows[0] if rows else None)
    _attach_creds(rows)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "models.html",
        _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected, test_result=None))


@panel_app.post("/models")
async def panel_model_create(
    request: Request, name: str = Form(...), provider: str = Form(...), version: str = Form(...),
    base_url: str = Form(""), api_key_env: str = Form(""), temperature: str = Form(""),
    max_output_tokens: str = Form(""), description: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Create a model row from the panel form (admin only)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    try:
        row = await model_service.create_model(
            session, name=name.strip(), provider=provider.strip(), version=version.strip(),
            base_url=base_url.strip() or None, api_key_env=api_key_env.strip() or None,
            temperature=float(temperature) if temperature.strip() else None,
            max_output_tokens=int(max_output_tokens) if max_output_tokens.strip() else None,
            description=description.strip() or None, is_active=False,
        )
    except ValueError:
        rows = await model_service.list_models(session)
        selected = next((m for m in rows if m.is_active), rows[0] if rows else None)
        _attach_creds(rows)
        counts_ctx = await _sidebar_counts(session)
        return templates.TemplateResponse(request, "models.html",
            _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected,
                 test_result=None, error="نام مدل تکراری است."), status_code=422)
    await audit_service.record_event(session, user.id, "model_create", None, f"name={name}")
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}/activate")
async def panel_model_activate(request: Request, model_id: int, session: AsyncSession = Depends(session_dep)):
    """Activate one model atomically (exactly one active row)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    row = await model_service.activate_model(session, row)
    await audit_service.record_event(session, user.id, "model_activate", None, f"model={row.name}")
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}")
async def panel_model_update(
    request: Request, model_id: int, base_url: str = Form(""),
    api_key_env: str = Form(""), temperature: str = Form(""),
    max_output_tokens: str = Form(""), description: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Update the selected model's connection fields (admin only; name fixed)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    row = await model_service.update_model(session, row, {
        "base_url": base_url.strip() or None,
        "api_key_env": api_key_env.strip() or None,
        "temperature": float(temperature) if temperature.strip() else None,
        "max_output_tokens": int(max_output_tokens) if max_output_tokens.strip() else None,
        "description": description.strip() or None,
    })
    await audit_service.record_event(session, user.id, "model_create", None, f"update model={row.name}")
    return RedirectResponse(url=f"/models?model_id={model_id}", status_code=302)


@panel_app.post("/models/{model_id}/test")
async def panel_model_test(request: Request, model_id: int, session: AsyncSession = Depends(session_dep)):
    """Probe one model with the shared 1-token helper; the key is never shown."""
    user = await require_login(request, session)

    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    ok, error = await model_service.test_model_connection(row)
    test_result = {"ok": ok, "model": row.name, "error": error}
    ok_msg = f"اتصال مدل {row.name} موفق بود." if ok else None
    missing_key = error == "no API key configured"
    await audit_service.record_event(session, user.id, "model_test", None, f"model={row.name} ok={ok}")
    rows = await model_service.list_models(session)
    selected = row
    _attach_creds(rows)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "models.html",
        _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected,
             test_result=test_result,
             error="کلید API پیکربندی نشده است." if (not ok and missing_key) else (None if ok else f"اتصال ناموفق: {error}"),
             ok=ok_msg))


@panel_app.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request, session: AsyncSession = Depends(session_dep)):
    """Show the active prompt editor, versions, and the JSON contract."""
    user = await require_login(request, session)

    rows = await prompt_service.list_prompts(session)
    selected = next((p for p in rows if p.is_active), rows[0] if rows else None)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "prompts.html",
        _ctx(request, user, active="prompts", **counts_ctx, prompts=rows, selected=selected))


@panel_app.post("/prompts")
async def panel_prompt_create(request: Request, version: str = Form(...), system_prompt: str = Form(...), session: AsyncSession = Depends(session_dep)):
    """Create a new prompt version as a new row (never edits in place)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    try:
        row = await prompt_service.create_prompt(
            session, version=version.strip(), system_prompt=system_prompt, is_active=False
        )
    except ValueError:
        rows = await prompt_service.list_prompts(session)
        selected = next((p for p in rows if p.is_active), rows[0] if rows else None)
        counts_ctx = await _sidebar_counts(session)
        return templates.TemplateResponse(request, "prompts.html",
            _ctx(request, user, active="prompts", **counts_ctx, prompts=rows, selected=selected,
                 error="نسخه تکراری است."), status_code=422)
    await audit_service.record_event(session, user.id, "prompt_create", None, f"version={version}")
    return RedirectResponse(url="/prompts", status_code=302)


@panel_app.post("/prompts/{prompt_id}/activate")
async def panel_prompt_activate(request: Request, prompt_id: int, session: AsyncSession = Depends(session_dep)):
    """Activate one prompt version atomically (previous stays as history)."""
    user = await require_login(request, session)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await prompt_service.get_prompt(session, prompt_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="پرامپت یافت نشد."), status_code=404)
    row = await prompt_service.activate_prompt(session, row)
    await audit_service.record_event(session, user.id, "prompt_activate", None, f"version={row.version}")
    return RedirectResponse(url="/prompts", status_code=302)


# --------------------------------------------------------------------------
# panel submit
# --------------------------------------------------------------------------


@panel_app.get("/submit", response_class=HTMLResponse)
async def submit_page(request: Request, session: AsyncSession = Depends(session_dep)):
    """Show the thesis submit form with model/prompt override selects."""
    user = await require_login(request, session)

    models = await model_service.list_models(session)
    prompts = await prompt_service.list_prompts(session)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "submit.html",
        _ctx(request, user, active="submit", **counts_ctx, models=models, prompts=prompts))


@panel_app.post("/submit")
async def submit_create(
    request: Request, username: str = Form(...), title: str = Form(...), abstract: str = Form(...),
    field: str = Form(...), university: str = Form(...), table_of_contents: str = Form(...),
    author: str = Form(...), supervisor: str = Form(...), model_id: str = Form(""), prompt_id: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Create a thesis job from the panel form (same flow as POST /submit)."""

    user = await require_login(request, session)
    if not _can_review(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    mid = int(model_id) if model_id.strip().isdigit() else None
    pid = int(prompt_id) if prompt_id.strip().isdigit() else None
    try:
        row = await record_service.create_thesis_record(
            session,
            username=username.strip(), title=title.strip(), abstract=abstract.strip(),
            field=field.strip(), university=university.strip(),
            table_of_contents=table_of_contents.strip(),
            author=author.strip(), supervisor=supervisor.strip(),
            model_id=mid, prompt_id=pid,
        )
        await audit_service.record_event(session, user.id, "submit", row.id, f"field={field}")
    except ValueError as e:
        message = {
            "model_id does not exist": "مدل انتخاب‌شده وجود ندارد.",
            "prompt_id does not exist": "پرامپت انتخاب‌شده وجود ندارد.",
        }.get(str(e), "مدل/پرامپت انتخاب‌شده وجود ندارد.")
        models = await model_service.list_models(session)
        prompts = await prompt_service.list_prompts(session)
        counts_ctx = await _sidebar_counts(session)
        return templates.TemplateResponse(request, "submit.html",
            _ctx(request, user, active="submit", **counts_ctx, models=models, prompts=prompts,
                 error=message),
            status_code=422)
    return RedirectResponse(url=f"/records/{row.id}", status_code=302)


# --------------------------------------------------------------------------
# users & audit (admin only; viewer/editor get 403)
# --------------------------------------------------------------------------


def _require_admin_or_403(request: Request, user):
    """Render the 403 page unless the user is an admin (management pages)."""
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    return None


@panel_app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, session: AsyncSession = Depends(session_dep)):
    """List console users with the role access matrix (admin only)."""
    user = await require_login(request, session)
    denied = _require_admin_or_403(request, user)
    if denied is not None:
        return denied

    users = await user_service.list_users(session)
    roles = await user_service.list_roles(session)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "users.html",
        _ctx(request, user, active="users", **counts_ctx, users=users, roles=roles))


@panel_app.post("/users")
async def user_create(
    request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...),
    session: AsyncSession = Depends(session_dep),
):
    """Create a console user with an argon2-hashed password (admin only)."""
    user = await require_login(request, session)
    denied = _require_admin_or_403(request, user)
    if denied is not None:
        return denied
    if role not in ("admin", "editor", "viewer"):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="نقش نامعتبر است."), status_code=422)

    try:
        row = await user_service.create_user_with_password(
            session, username=username.strip(), password=password, role=role,
        )
    except ValueError as e:
        message = {
            "weak-password": "گذرواژه باید حداقل ۸ نویسه باشد.",
            "duplicate-username": "نام کاربری تکراری است.",
        }.get(str(e), "نقش یافت نشد.")
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=422)
    await audit_service.record_event(session, user.id, "user_create", None, f"username={username}")
    return RedirectResponse(url="/users", status_code=302)


@panel_app.post("/users/{user_id}/role")
async def user_change_role(request: Request, user_id: int, role: str = Form(...), session: AsyncSession = Depends(session_dep)):
    """Change a console user's role (admin only)."""
    user = await require_login(request, session)
    denied = _require_admin_or_403(request, user)
    if denied is not None:
        return denied
    if role not in ("admin", "editor", "viewer"):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="نقش نامعتبر است."), status_code=422)

    row = await user_service.get_user(session, user_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کاربر یافت نشد."), status_code=404)
    try:
        row = await user_service.change_role(session, row, role)
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="نقش یافت نشد."), status_code=422)
    await audit_service.record_event(session, user.id, "user_role", None, f"username={row.username} role={role}")
    return RedirectResponse(url="/users", status_code=302)


@panel_app.post("/users/{user_id}/toggle")
async def user_toggle(request: Request, user_id: int, session: AsyncSession = Depends(session_dep)):
    """Activate/deactivate a console user (admin only; never deletes)."""
    user = await require_login(request, session)
    denied = _require_admin_or_403(request, user)
    if denied is not None:
        return denied

    row = await user_service.get_user(session, user_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کاربر یافت نشد."), status_code=404)
    try:
        row = await user_service.toggle_user(session, row, actor_id=user.id)
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="نمی‌توانید حساب خودتان را غیرفعال کنید."), status_code=422)
    await audit_service.record_event(session, user.id, "user_toggle", None, f"username={row.username} active={row.is_active}")
    return RedirectResponse(url="/users", status_code=302)


@panel_app.post("/users/{user_id}/password")
async def user_reset_password(request: Request, user_id: int, new_password: str = Form(...), session: AsyncSession = Depends(session_dep)):
    """Set any user's password (admin only; audited, never stored plaintext)."""
    user = await require_login(request, session)
    denied = _require_admin_or_403(request, user)
    if denied is not None:
        return denied

    row = await user_service.get_user(session, user_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کاربر یافت نشد."), status_code=404)
    try:
        row = await user_service.reset_password(session, row, new_password)
    except ValueError as e:
        message = "گذرواژه باید حداقل ۸ نویسه باشد." if str(e) == "weak-password" else "تغییر گذرواژه ممکن نیست."
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=422)
    await audit_service.record_event(session, user.id, "password_reset", None, f"username={row.username}")
    return RedirectResponse(url="/users", status_code=302)


@panel_app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, session: AsyncSession = Depends(session_dep)):
    """Show the logged-in user's own account (every role)."""
    user = await require_login(request, session)
    target = await user_service.get_active_user_with_role(session, user.id)
    counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "profile.html",
        _ctx(request, user, active="", **counts_ctx, target=target or user))


@panel_app.post("/profile/password")
async def profile_change_password(
    request: Request, current_password: str = Form(...), new_password: str = Form(...),
    session: AsyncSession = Depends(session_dep),
):
    """Let any user change their own password after proving the current one."""
    user = await require_login(request, session)
    row = await user_service.get_user(session, user.id)
    counts_ctx = await _sidebar_counts(session)
    if row is None:
        return templates.TemplateResponse(request, "profile.html",
            _ctx(request, user, active="", **counts_ctx, target=user,
                 error="کاربر یافت نشد."), status_code=404)
    try:
        row = await user_service.change_own_password(
            session, row, current_password=current_password, new_password=new_password
        )
    except ValueError as e:
        status = 401 if str(e) == "bad-current-password" else 422
        message = (
            "گذرواژهٔ فعلی نادرست است." if status == 401
            else "گذرواژه باید حداقل ۸ نویسه باشد."
        )
        return templates.TemplateResponse(request, "profile.html",
            _ctx(request, user, active="", **counts_ctx, target=user, error=message),
            status_code=status)
    await audit_service.record_event(session, user.id, "password_change", None, f"username={row.username}")
    return templates.TemplateResponse(request, "profile.html",
        _ctx(request, user, active="", target=row, ok="گذرواژه با موفقیت تغییر کرد."))


@panel_app.post("/users/{user_id}/delete")
async def user_delete(request: Request, user_id: int, session: AsyncSession = Depends(session_dep)):
    """Delete a console user with safeguards (admin only).

    Deactivation stays the recommended action (see the toggle button and the
    hint on the page). Deletion keeps history-bearing rows as deactivated
    rows and hard-deletes only users with no audit trail.
    """
    user = await require_login(request, session)
    denied = _require_admin_or_403(request, user)
    if denied is not None:
        return denied

    row = await user_service.get_user(session, user_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کاربر یافت نشد."), status_code=404)
    username = row.username
    try:
        outcome = await user_service.delete_user(session, row, actor_id=user.id)
    except ValueError as e:
        message = {
            "self-delete": "نمی‌توانید حساب خودتان را حذف کنید.",
            "last-admin": "آخرین ادمین فعال قابل حذف نیست.",
        }.get(str(e), "حذف ممکن نیست.")
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=422)
    await audit_service.record_event(session, user.id, "user_delete", None, f"username={username} outcome={outcome}")
    return RedirectResponse(url="/users", status_code=302)


@panel_app.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request, action: str = "", username: str = "", main_id: str = "", page: int = 1,
    session: AsyncSession = Depends(session_dep),
):
    """Show the immutable audit trail with filters (admin only)."""
    user = await require_login(request, session)
    denied = _require_admin_or_403(request, user)
    if denied is not None:
        return denied
    page = max(1, page)
    page_size = 20
    action_f = action or None
    user_f = username.strip() or None
    mid = int(main_id.strip()) if main_id.strip().isdigit() else None

    events, total = await user_service.audit_trail(
        session, action=action_f, username=user_f, main_id=mid,
        page=page, page_size=page_size,
    )
    for event in events:
        event["action"] = _ACTION_FA.get(event["action"], event["action"])
    users = await user_service.audit_usernames(session)
    counts_ctx = await _sidebar_counts(session)
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in {"action": action, "username": username, "main_id": main_id}.items() if v})
    return templates.TemplateResponse(request, "audit.html",
        _ctx(request, user, active="audit", **counts_ctx, events=events, total=total, page=page,
             page_size=page_size, action=action, username=username, main_id=main_id, query=query,
             users=users, actions=sorted(_ACTION_FA.items())))
