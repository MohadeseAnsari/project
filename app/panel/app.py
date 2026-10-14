"""Server-rendered review panel (Jinja2 + locally vendored HTMX, no build step).

Same image as the API with a different command (``web`` compose service).
Persian RTL UI, plain CSS, Vazirmatn fonts vendored under ``/static``.
Human auth uses panel users + signed cookies (not the machine shared secret);
every review/retry/config change is written to ``audit_logs`` for history.
"""

import logging
import time
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
from app.panel.authz import Action, can, require
from app.panel.csrf import CSRFError, csrf_field, verify_csrf
from app.services import audit as audit_service
from app.services import dashboard as dashboard_service
from app.services.errors import PermissionDenied, from_value_error
from app.services import channels as channel_service
from app.services import deliveries as delivery_service
from app.services import dispose_engine
from app.services import fields as field_service
from app.services import models as model_service
from app.services import prompts as prompt_service
from app.services import records as record_service
from app.services import session_dep as service_session_dep
from app.services import users as user_service
from app.services.transitions import STATUS_FA

_here = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_here / "templates"))


def _service_error(exc: ValueError, default: str = "") -> str:
    """Persian text for a service ``ValueError`` whose ``str()`` is a code.

    Delegates to the one lookup table in ``app/services/errors`` so the panel
    and the machine API describe the same failure identically, and no
    traceback ever reaches the operator.
    """
    return from_value_error(exc, default or None)[1]


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




def _role_name(user) -> str | None:
    """The acting user's role name, or None when unauthenticated/unknown."""
    return user.role.name if user is not None and user.role is not None else None


def _is_admin(user) -> bool:
    """Whether the user holds the admin role (thin wrapper over the policy)."""
    return can(_role_name(user), Action.MANAGE_CONFIG)


def _can_review(user) -> bool:
    """Review rights (approve/reject/retry/submit) per the role policy.

    Thin wrappers over :mod:`app.panel.authz` — the policy lives in exactly
    one place, so routes cannot drift apart in what they allow.
    """
    return can(_role_name(user), Action.REVIEW_RECORD)


def _require_role(user, action: Action) -> None:
    """Raise :class:`PermissionDenied` when the user may not perform ``action``."""
    require(_role_name(user), action)


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

    live = await dashboard_service.live_counters(session)
    refresh_seconds = settings.panel_dashboard_refresh_seconds

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

    delivery_counts = await delivery_service.delivery_status_counts(session)
    failed_note = None
    if delivery_counts.get("failed", 0):
        failed_note = f"{_fa(delivery_counts['failed'])} تحویل ناموفق در صف اقدام دستی است؛ از صفحهٔ ارسال‌ها «تلاش مجدد» را بزنید."

    counts_ctx = await dashboard_service.sidebar_badges(session)
    now_label = jdatetime.datetime.now().strftime("%H:%M")
    return templates.TemplateResponse(request, "dashboard.html",
        _ctx(
            request, user, active="dashboard", **counts_ctx, **live,
            chart=chart, timeline=timeline, channels=channels, failed_note=failed_note,
            now_label=now_label, refresh_seconds=refresh_seconds,
        ),
    )


@panel_app.get("/dashboard/live", response_class=HTMLResponse)
async def dashboard_live(request: Request, session: AsyncSession = Depends(session_dep)):
    """The dashboard's counters and health card, for HTMX polling (item 19).

    Only the volatile region: four GROUP BY counts, the worker leases and the
    active model's connection state. The 7-day chart and the event timeline
    are deliberately excluded — they do not change over the poll window, and
    re-rendering them would only add flicker.
    """
    user = await require_login(request, session)
    live = await dashboard_service.live_counters(session)
    sidebar = await dashboard_service.sidebar_badges(session)
    return templates.TemplateResponse(
        request, "dashboard_live.html",
        _ctx(
            request, user, **sidebar, **live,
            refresh_seconds=settings.panel_dashboard_refresh_seconds,
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
    tab: str = "", platform: str = "", main_id: str = "",
):
    """Records, review AND deliveries on one page (item 2).

    A record and its deliveries are one thing seen from two sides, and the
    operator moves between "what needs my decision" and "did it reach the
    channel" constantly — so they are two tabs here rather than two pages with
    two filter bars. ``/deliveries`` redirects to the deliveries tab.

    The deliveries tab carries its own filters (``d*`` names) because they are
    genuinely different axes: a record filters by field and university, a
    delivery by platform and outcome.
    """
    user = await require_login(request, session)
    from urllib.parse import urlencode

    if tab == "deliveries":
        return await _deliveries_tab(
            request, session, status=status, platform=platform, main_id=main_id,
        )
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
    counts_ctx = await dashboard_service.sidebar_badges(session)
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
    # Timeline of the real flow (submitted -> resolved -> generated ->
    # reviewed -> delivered), built from columns and audit rows only.
    timeline = [
        {"title": step["title"], "sub": f"{step['sub']} · {_jalali(step['at'])}" if step["at"] else step["sub"]}
        for step in await record_service.record_timeline(session, record)
    ]
    categories = await field_service.list_categories(session)
    counts_ctx = await dashboard_service.sidebar_badges(session)
    # How many messages are really out there, for the bulk delete button.
    published_count = sum(1 for d in deliveries if d.status == "sent")
    delete_note = {
        "all": "پیام این خبر از همهٔ کانال‌ها حذف شد.",
        "partial": "بعضی کانال‌ها پیام را حذف نکردند و وضعیتشان «ارسال‌شده» ماند؛ در ستون شناسهٔ پیام قابل تلاش مجدد هستند.",
        "none": "هیچ پیام منتشرشده‌ای برای حذف نبود.",
    }.get(request.query_params.get("deleted", ""), None)
    return templates.TemplateResponse(request, "record_detail.html",
        _ctx(
            request, user, active="records", **counts_ctx, record=record, deliveries=deliveries,
            history=history, tab=tab, model_name=model_name, prompt_version=prompt_version,
            approved_by=approved_by, timeline=timeline, categories=categories,
            routed_channels=routed_channels, message_preview=message_preview,
            snapshot_category=overview["snapshot_category"],
            current_category=overview["current_category"],
            field_keyword=overview["field_keyword"], prompt_preview=prompt_preview,
            published_count=published_count, delete_note=delete_note,
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
    try:
        _require_role(user, Action.REVIEW_RECORD)
    except PermissionDenied:
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
    try:
        _require_role(user, Action.REVIEW_RECORD)
    except PermissionDenied:
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


@panel_app.post("/records/{record_id}/category")
async def record_reassign_category(
    request: Request, record_id: int, category_id: int = Form(...),
    session: AsyncSession = Depends(session_dep),
):
    """Re-point one record at another category (operator correction)."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.REVIEW_RECORD)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    record = await record_service.get_record(session, record_id)
    if record is None:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رکورد یافت نشد."), status_code=404)
    try:
        await record_service.reassign_category(session, record, category_id)
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دستهٔ انتخاب‌شده وجود ندارد."), status_code=422)
    await audit_service.record_event(
        session, user.id, "reassign_category", record.id,
        f"category={category_id}",
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
    try:
        _require_role(user, Action.REVIEW_RECORD)
    except PermissionDenied:
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


@panel_app.get("/deliveries")
async def deliveries_page_merged(request: Request):
    """The deliveries page is merged into ``/records?tab=deliveries`` (item 2)."""
    return RedirectResponse(url="/records?tab=deliveries", status_code=301)


async def _deliveries_tab(
    request: Request, session: AsyncSession, *, status: str = "",
    platform: str = "", main_id: str = "",
):
    """Render the deliveries half of the merged page (item 2).

    Same data and same filters the standalone page used, so the HTMX partial
    at /deliveries/table keeps working unchanged.
    """
    user = await require_login(request, session)
    from urllib.parse import urlencode

    page_size = 20
    mid = int(main_id.strip()) if main_id.strip().isdigit() else None
    pairs, total = await delivery_service.fetch_deliveries(
        session, page=1, page_size=page_size, status=status or None,
        platform=platform or None, main_id=mid,
    )
    items = await delivery_service.delivery_items(session, pairs)
    newest_failure = await delivery_service.last_failed(session)
    last_error = (
        f"خطای آخر: «{newest_failure.error_message}» — {_jalali(newest_failure.updated_at)}"
        if newest_failure and newest_failure.error_message else None
    )
    counts = await record_service.record_status_counts(session)
    delivery_counts = await delivery_service.delivery_status_counts(session)
    needs_review = await record_service.needs_review_count(session)
    options = await record_service.filter_options(session)
    dquery = urlencode(
        {k: v for k, v in {"status": status, "platform": platform, "main_id": main_id}.items() if v}
    )
    counts_ctx = await dashboard_service.sidebar_badges(session)
    return templates.TemplateResponse(request, "records.html",
        _ctx(
            request, user, active="deliveries", **counts_ctx, tab="deliveries",
            dstatus=status, dplatform=platform, dmain_id=main_id, dquery=dquery,
            total=total, items=items, page=1, page_size=page_size, last_error=last_error,
            max_attempts=settings.sender_max_attempts,
            backoff_base=settings.sender_backoff_base_seconds,
            failed_count=delivery_counts.get("failed", 0),
            # The KPI strip is shared by both tabs, so it is always supplied.
            kpi_review=needs_review,
            kpi_queued=counts.get("ready", 0) + counts.get("processing", 0),
            kpi_sent=delivery_counts.get("sent", 0),
            kpi_failed=counts.get("failed", 0) + delivery_counts.get("failed", 0),
            field_options=options["fields"], university_options=options["universities"],
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
    try:
        _require_role(user, Action.RETRY_DELIVERY)
    except PermissionDenied:
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
    try:
        _require_role(user, Action.RETRY_DELIVERY)
    except PermissionDenied:
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


@panel_app.post("/deliveries/{delivery_id}/cancel")
async def delivery_cancel(
    request: Request, delivery_id: int, session: AsyncSession = Depends(session_dep)
):
    """Cancel a not-yet-sent delivery (wrong channel, unwanted send).

    Parked as ``failed`` with an operator note: it stays visible and can be
    retried, instead of disappearing from the queue without trace.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.RETRY_DELIVERY)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    delivery = await delivery_service.get_delivery(session, delivery_id)
    if delivery is None:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال یافت نشد."), status_code=404)
    try:
        delivery = await delivery_service.cancel_delivery(session, delivery)
    except ValueError:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال در حال ارسال یا ارسال‌شده قابل لغو نیست."), status_code=409)
    await audit_service.record_event(session, user.id, "cancel", delivery.main_id, f"delivery={delivery.id}")
    return RedirectResponse(url="/deliveries", status_code=302)


@panel_app.post("/deliveries/{delivery_id}/delete-from-channel")
async def delivery_delete_from_channel(
    request: Request, delivery_id: int, session: AsyncSession = Depends(session_dep)
):
    """Delete the published message IN THE CHANNEL, then record it.

    This calls the platform's ``deleteMessage`` through the channel's own bot
    token, so the article really disappears; only after the platform confirms
    does the row move to ``deleted_from_channel`` (keeping the message id as
    history). A refusal leaves the row ``sent`` and shows the real reason, so
    the operator knows the message is still there.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.RETRY_DELIVERY)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    delivery = await delivery_service.get_delivery(session, delivery_id)
    if delivery is None:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال یافت نشد."), status_code=404)
    try:
        delivery = await delivery_service.delete_published_message(session, delivery)
    except ValueError as e:
        status, message = from_value_error(e, "حذف پیام ممکن نشد.")
        await audit_service.record_event(session, user.id, "delete_from_channel_failed", delivery.main_id,
                                         f"delivery={delivery.id} error={e}")
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error=message),
                                          status_code=status)
    await audit_service.record_event(
        session, user.id, "delete_from_channel", delivery.main_id,
        f"delivery={delivery.id} message_id={delivery.platform_message_id}",
    )
    return RedirectResponse(url="/deliveries", status_code=303)


@panel_app.post("/records/{record_id}/delete-everywhere")
async def record_delete_everywhere(
    request: Request, record_id: int, confirm: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Delete one article's published message from EVERY channel it reached.

    An article fans out to a delivery row per routed channel, so removing
    "the news" means acting on all of them. Each channel is a separate
    platform call and they are handled independently: a channel that refuses
    does not stop the others, and its row stays ``sent`` so the operator can
    see the message is still there and retry just that one.

    Requires an explicit ``confirm`` — this is a bulk, irreversible removal.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.RETRY_DELIVERY)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    article = await record_service.get_record(session, record_id)
    if article is None:
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error="رکورد یافت نشد."), status_code=404)
    if confirm.strip() != "yes":
        return RedirectResponse(url=f"/records/{record_id}?tab=delivery", status_code=303)

    report = await delivery_service.delete_article_everywhere(session, record_id)
    await audit_service.record_event(
        session, user.id, "delete_from_channel", record_id,
        f"record={record_id} deleted={len(report['deleted'])} "
        f"already_gone={len(report['already_gone'])} failed={len(report['failed'])}",
    )
    for name, code in report["failed"]:
        # The per-channel reason (refused/unavailable/no-token) is what the
        # operator needs to know which message is still out there.
        await audit_service.record_event(session, user.id, "delete_from_channel_failed", record_id,
                                         f"record={record_id} channel={name} error={code}")
    notice = _delete_notice(report)
    return RedirectResponse(url=f"/records/{record_id}?tab=delivery&deleted={notice}",
                            status_code=303)


def _delete_notice(report: dict) -> str:
    """One short, safe summary of a fan-out delete for the redirect note.

    Only a flag: no platform error text is carried in a query string, and the
    per-channel detail lives in the audit log.
    """
    if report["failed"]:
        return "partial"
    if report["deleted"] or report["already_gone"]:
        return "all"
    return "none"


@panel_app.post("/deliveries/{delivery_id}/mark-deleted")
async def delivery_mark_deleted(
    request: Request, delivery_id: int, session: AsyncSession = Depends(session_dep)
):
    """Record ``deleted_from_channel`` when the message was already removed by hand.

    The companion action for when the operator deleted the post in the channel
    app: nothing is sent to the platform, the row just reflects reality.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.RETRY_DELIVERY)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)
    delivery = await delivery_service.get_delivery(session, delivery_id)
    if delivery is None:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال یافت نشد."), status_code=404)
    try:
        delivery = await delivery_service.mark_deleted_from_channel(session, delivery)
    except ValueError as e:
        status, message = from_value_error(e)
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error=message),
                                          status_code=status)
    await audit_service.record_event(
        session, user.id, "mark_deleted", delivery.main_id,
        f"delivery={delivery.id} message_id={delivery.platform_message_id}",
    )
    return RedirectResponse(url="/deliveries", status_code=303)


# --------------------------------------------------------------------------
# reference data
# --------------------------------------------------------------------------


@panel_app.get("/fields", response_class=HTMLResponse)
async def fields_page(
    request: Request, tab: str = "", q: str = "",
    session: AsyncSession = Depends(session_dep),
):
    """Fields, categories and keywords on ONE page (item 4).

    They were three pages because they were written at different times, not
    because the concepts differ: a field is a row, its category is a column on
    that row, and the matched keyword is why the column holds what it holds.
    One page with two tabs, and the field table carries the category as an
    editable select so a change and its consequence are visible together.

    ``/categories`` redirects here rather than 404ing, so old links survive.
    """
    user = await require_login(request, session)
    if tab not in ("", "categories"):
        tab = ""

    fields = await field_service.list_fields(session)
    total = await field_service.field_total(session)
    top = await field_service.top_fields(session)
    categories, category_total = await field_service.category_cards(session, q)
    if q.strip():
        # The search box filters whatever tab is open, so the category tab must
        # search categories while the field tab searches fields.
        fields = [f for f in fields if q.strip() in f["name"]]
    channels = await channel_service.list_channels(session)
    counts_ctx = await dashboard_service.sidebar_badges(session)
    return templates.TemplateResponse(request, "fields.html",
        _ctx(request, user, active="fields", **counts_ctx, fields=fields, total=total, top=top,
             tab=tab, q=q, categories=categories, category_total=category_total,
             channels=channels))


@panel_app.get("/categories")
async def categories_page_merged(request: Request, session: AsyncSession = Depends(session_dep)):
    """The standalone categories page is merged into ``/fields`` (item 4)."""
    return RedirectResponse(url="/fields?tab=categories", status_code=301)


@panel_app.post("/fields/{field_id}/confirm")
async def field_confirm(request: Request, field_id: int, session: AsyncSession = Depends(session_dep)):
    """Freeze a field's category as human-curated (reviewers and admins)."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.REVIEW_RECORD)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="حساب بیننده فقط خواندنی است."), status_code=403)

    field = await field_service.get_field(session, field_id)
    if not field:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رشته یافت نشد."), status_code=404)
    field = await field_service.confirm_category(session, field)
    await audit_service.record_event(session, user.id, "curate", None, f"field={field.name}")
    return RedirectResponse(url="/fields", status_code=303)


@panel_app.get("/channels", response_class=HTMLResponse)
async def channels_page(
    request: Request, channel_id: int | None = None, new: str = "",
    fq: str = "", field_page: int = 1,
    session: AsyncSession = Depends(session_dep),
):
    """Channels, plus the two assignments the owner configures (item 6).

    No route MATRIX here any more. Delivery broadcasts to every active
    channel, so what this page configures is (1) which category each field
    belongs to and (2) which channel is each category's designated news
    channel. A channel is kept out of delivery by switching it off.
    """
    user = await require_login(request, session)

    channels = await channel_service.channels_with_route_counts(session)
    selected = None
    if channel_id is not None:
        selected = await channel_service.get_channel(session, channel_id)
    if selected is None:
        selected = channels[0] if channels else None
    categories = await channel_service.category_channel_matrix(session)
    # The field list is searched and paginated: a deployment has hundreds of
    # fields, and rendering every one of them made the page unusable.
    field_rows, field_total = await field_service.field_category_rows(
        session, q=fq, page=field_page
    )
    field_page_size = 50
    usage = await channel_service.channel_usage(session)
    cat_channel_counts = {
        row["id"]: sum(1 for c in categories if c["channel_id"] == row["id"])
        for row in categories
    }
    for channel in channels:
        channel.usage = usage.get(
            channel.id,
            {"deliveries": 0, "sent": 0, "field_routes": 0, "category_routes": 0},
        )
        channel.category_count = cat_channel_counts.get(channel.id, 0)
    counts_ctx = await dashboard_service.sidebar_badges(session)
    return templates.TemplateResponse(request, "channels.html",
        _ctx(request, user, active="channels", **counts_ctx, channels=channels, selected=selected,
             categories=categories, field_rows=field_rows, field_q=fq,
             field_page=field_page, field_page_size=field_page_size,
             field_total=field_total, active_count=sum(1 for c in channels if c.is_active),
             # With no channels at all there is nothing to edit, so the empty
             # state must offer the add form — otherwise the page has no form
             # on it and a first-time operator is stuck.
             show_new=bool(new) or not channels))



@panel_app.get("/channels/table", response_class=HTMLResponse)
async def channels_fields_table(
    request: Request, fq: str = "", page: int = 1,
    session: AsyncSession = Depends(session_dep),
):
    """Just the searchable field → category table, for HTMX paging.

    Only the table swaps, so paging does not throw the operator back to the top
    of a long page and lose the channel they were editing.
    """
    user = await require_login(request, session)
    field_rows, field_total = await field_service.field_category_rows(
        session, q=fq, page=page
    )
    categories, _total = await field_service.category_cards(session)
    return templates.TemplateResponse(
        request, "channels_fields_table.html",
        _ctx(
            request, user, field_rows=field_rows, field_q=fq, field_page=page,
            field_page_size=50, field_total=field_total, categories=categories,
        ),
    )


@panel_app.get("/routing")
async def routing_page_removed(request: Request):
    """The routing-preview page is gone (item 5).

    Its old address redirects to the channels page, which is where the
    field/category/channel configuration lives now, so bookmarks and any link
    typed earlier keep working instead of 404ing.
    """
    return RedirectResponse(url="/channels", status_code=301)


@panel_app.post("/channels/category-channel")
async def channel_set_category_channel(
    request: Request, category_id: int = Form(...), channel_id: int = Form(0),
    session: AsyncSession = Depends(session_dep),
):
    """Record which channel is a category's designated news channel (item 6).

    Delivery broadcasts to every active channel, so this pairing is a label and
    a provenance marker rather than a filter. ``channel_id=0`` clears it.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    try:
        outcome = await channel_service.set_category_channel(
            session, category_id=category_id, channel_id=channel_id or None
        )
    except ValueError as e:
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=_service_error(e, "تعیین کانال دسته ممکن نشد.")),
            status_code=422)
    await audit_service.record_event(session, user.id, "route", None,
                                     f"category={category_id} channel={channel_id or 'none'} {outcome}")
    return RedirectResponse(url="/fields", status_code=303)


@panel_app.post("/fields/{field_id}/category")
async def field_set_category(
    request: Request, field_id: int, category_id: int = Form(...),
    return_to: str = Form("fields"),
    session: AsyncSession = Depends(session_dep),
):
    """Pin a field to a category once, by hand (item 15).

    The assignment is stored on the row and marked ``manual``, so the
    automatic ruleset does not quietly overwrite an operator's decision on the
    next classification pass. ``return_to`` sends the operator back to whichever
    page they made the edit from — both the channels page and the merged
    fields page offer this control.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.FIELD_CURATE)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی لازم را ندارید."), status_code=403)
    row = await field_service.get_field(session, field_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رشته یافت نشد."), status_code=404)
    try:
        row = await field_service.assign_field_category(session, row, category_id)
    except ValueError as e:
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=_service_error(e, "تعیین دسته ممکن نشد.")),
            status_code=422)
    await audit_service.record_event(session, user.id, "route", None,
                                     f"field={field_id} category={row.category_id} source=manual")
    # Whitelist rather than echoing the value: an open redirect here would let
    # a crafted form bounce an admin to another site.
    destination = "/channels" if return_to == "channels" else "/fields"
    return RedirectResponse(url=destination, status_code=303)


@panel_app.post("/channels/{channel_id}/toggle")
async def channel_toggle(request: Request, channel_id: int, session: AsyncSession = Depends(session_dep)):
    """Flip a channel's active switch (admin only).

    Switching a channel OFF is now the only way to stop delivery to it, since
    every active channel is a target (item 6).
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await channel_service.get_channel(session, channel_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کانال یافت نشد."), status_code=404)
    row = await channel_service.toggle_channel(session, row)
    await audit_service.record_event(session, user.id, "channel_toggle", None, f"channel={channel_id} active={row.is_active}")
    return RedirectResponse(url=f"/channels?channel_id={channel_id}", status_code=303)


@panel_app.post("/channels/{channel_id}")
async def channel_update(
    request: Request, channel_id: int, title: str = Form(""),
    chat_id: str = Form(""), username: str = Form(""), bot_token_env: str = Form(""),
    bot_token: str = Form(""), clear_token: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Edit ONE channel (admin only): title, destination, and its own token.

    The form is scoped to ``channel_id``, so saving leaves every other channel
    untouched. A blank ``bot_token`` keeps the stored one; ``clear_token``
    drops it and falls back to the platform-wide token.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await channel_service.get_channel(session, channel_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کانال یافت نشد."), status_code=404)
    updates = {"title": title.strip() or None, "username": username.strip() or None,
               "bot_token_env": bot_token_env.strip() or None}
    if chat_id.strip().lstrip("-").isdigit():
        updates["chat_id"] = int(chat_id.strip())
    try:
        row = await channel_service.update_channel(session, row, updates)
        if bot_token.strip():
            row = await channel_service.set_channel_token(session, row, bot_token.strip())
        if clear_token.strip():
            row = await channel_service.clear_channel_token(session, row)
    except ValueError as e:
        message = _service_error(e, default="ذخیرهٔ کانال ناموفق بود.")
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=422)
    await audit_service.record_event(session, user.id, "channel_update", None,
                                     f"channel={channel_id} token={'set' if bot_token.strip() else 'unchanged'}")
    return RedirectResponse(url=f"/channels?channel_id={channel_id}", status_code=302)


@panel_app.post("/channels/{channel_id}/delete")
async def channel_delete(
    request: Request, channel_id: int, confirm: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Delete a channel that never received anything; otherwise disable it.

    Requires an explicit ``confirm`` so a stray click cannot drop a
    destination. A channel with delivery history is turned off instead of
    deleted, because those rows must keep resolving.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await channel_service.get_channel(session, channel_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کانال یافت نشد."), status_code=404)
    if confirm.strip() != "yes":
        return RedirectResponse(url=f"/channels?channel_id={channel_id}", status_code=303)
    outcome = await channel_service.delete_channel(session, row)
    await audit_service.record_event(session, user.id, "channel_delete", None, f"channel={channel_id} outcome={outcome}")
    return RedirectResponse(url="/channels", status_code=303)


@panel_app.post("/channels")
async def channel_create(
    request: Request, platform: str = Form(...), target: str = Form(...),
    title: str = Form(""), bot_token_env: str = Form(""), bot_token: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """ADD a channel (admin only): a new row beside the existing ones.

    Accepts a numeric chat id or an ``@username``; the platform API resolves
    it first, so a typo fails here instead of parking every future send. The
    bot token can be typed straight into the form (stored encrypted, never
    shown again), so adding a channel needs no environment change.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    target = target.strip()
    try:

        row = await channel_service.create_channel(
            session, platform=platform, target=target,
            title=title.strip() or None,
            bot_token_env=bot_token_env.strip() or None,
            bot_token=bot_token.strip() or None,
        )
        await audit_service.record_event(session, user.id, "channel_create", None, f"{platform}:{row.chat_id}")
    except ValueError as e:
        code = str(e)
        if code == "no-platform-token":
            # Names the platform so the operator knows which token is missing.
            message = (f"توکن ربات {platform} نه در فرم و نه در محیط پیکربندی نشده است؛ "
                       "توکن را در فرم وارد کنید.")
        else:
            message = _service_error(e, default=f"کانال یافت نشد: {code}")
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=422)
    return RedirectResponse(url=f"/channels?channel_id={row.id}", status_code=303)




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
    counts_ctx = await dashboard_service.sidebar_badges(session)
    return templates.TemplateResponse(request, "models.html",
        _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected, test_result=None))


@panel_app.post("/models")
async def panel_model_create(
    request: Request, name: str = Form(...), provider: str = Form(...), version: str = Form(...),
    base_url: str = Form(""), api_key_env: str = Form(""), temperature: str = Form(""),
    max_output_tokens: str = Form(""), description: str = Form(""), api_key: str = Form(""),
    api_model: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Create a model row from the panel form (admin only).

    A pasted ``api_key`` is encrypted before it reaches the row and is never
    echoed back. A row with its own ``base_url`` requires its own key: the
    global key is never sent to an endpoint the row introduced.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    try:
        row = await model_service.create_model(
            session, name=name.strip(), provider=provider.strip(), version=version.strip(),
            base_url=base_url.strip() or None, api_key_env=api_key_env.strip() or None,
            temperature=float(temperature) if temperature.strip() else None,
            max_output_tokens=int(max_output_tokens) if max_output_tokens.strip() else None,
            description=description.strip() or None, is_active=False,
            api_key=api_key.strip() or None, api_model=api_model.strip() or None,
        )
    except ValueError as exc:
        message = {
            "duplicate-name": "نام مدل تکراری است.",
            "bad-credential": "برای مدلی با نقطهٔ اتصال اختصاصی، کلید خودش لازم است؛ کلید سراسری به این آدرس ارسال نمی‌شود.",
        }.get(str(exc), "ایجاد مدل ناموفق بود.")
        rows = await model_service.list_models(session)
        selected = next((m for m in rows if m.is_active), rows[0] if rows else None)
        _attach_creds(rows)
        counts_ctx = await dashboard_service.sidebar_badges(session)
        return templates.TemplateResponse(request, "models.html",
            _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected,
                 test_result=None, error=message), status_code=422)
    await audit_service.record_event(
        session, user.id, "model_create", None,
        f"name={name} base_url={'yes' if base_url.strip() else 'no'} key={'set' if api_key.strip() else 'none'}",
    )
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}/clear-key")
async def panel_model_clear_key(
    request: Request, model_id: int, session: AsyncSession = Depends(session_dep)
):
    """Remove a model's stored key (the explicit "پاک کردن کلید" action)."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    await model_service.clear_api_key(session, row)
    await audit_service.record_event(session, user.id, "model_clear_key", None, f"model={row.name}")
    return RedirectResponse(url=f"/models?model_id={model_id}", status_code=302)


@panel_app.post("/models/{model_id}/deactivate")
async def panel_model_deactivate(
    request: Request, model_id: int, session: AsyncSession = Depends(session_dep)
):
    """Deactivate a model, refusing to leave zero usable models."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    try:
        await model_service.deactivate_model(session, row)
    except ValueError as exc:
        message = "این تنها مدل قابل استفاده است؛ حذف یا غیرفعال‌سازی آن تولید را متوقف می‌کند." if str(exc) == "last-usable-model" else "غیرفعال‌سازی ناموفق بود."
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=422)
    await audit_service.record_event(session, user.id, "model_deactivate", None, f"model={row.name}")
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}/delete")
async def panel_model_delete(
    request: Request, model_id: int, session: AsyncSession = Depends(session_dep)
):
    """Delete an inactive model, with the "never leave zero usable" guard."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    name = row.name
    try:
        await model_service.delete_model(session, row)
    except ValueError as exc:
        message = {
            "model-active": "مدل فعال قابل حذف نیست؛ ابتدا غیرفعال کنید.",
            "last-usable-model": "این تنها مدل قابل استفاده است؛ حذف آن تولید را متوقف می‌کند.",
            "model-in-use": "این مدل در رکوردهای تولیدشده استفاده شده و برای حفظ تاریخچه حذف نمی‌شود.",
        }.get(str(exc), "حذف مدل ناموفق بود.")
        return templates.TemplateResponse(request, "base.html",
            _ctx(request, user, error=message), status_code=422)
    await audit_service.record_event(session, user.id, "model_delete", None, f"model={name}")
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}/activate")
async def panel_model_activate(request: Request, model_id: int, session: AsyncSession = Depends(session_dep)):
    """Activate one model atomically (exactly one active row).

    A model is only activated once it has actually answered a connection
    test: activating a row that was never verified is what produces a runtime
    "Requested resource <name> does not exist" from the provider, which the
    owner sees as a failed generation instead of a fixable settings problem.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    if row.last_tested_at is None or not row.last_test_ok:
        return templates.TemplateResponse(
            request, "base.html",
            _ctx(request, user, error=(
                "این مدل هنوز با موفقیت آزموده نشده است. ابتدا «تست اتصال» را بزنید؛ "
                "فعال‌سازی مدلی که ارائه‌دهنده نمی‌شناسد باعث خطای ۴۰۴ هنگام تولید خبر می‌شود."
            )),
            status_code=409,
        )
    row = await model_service.activate_model(session, row)
    await audit_service.record_event(session, user.id, "model_activate", None, f"model={row.name}")
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}")
async def panel_model_update(
    request: Request, model_id: int, base_url: str = Form(""),
    api_key_env: str = Form(""), temperature: str = Form(""),
    max_output_tokens: str = Form(""), description: str = Form(""),
    api_key: str = Form(""), api_model: str = Form(""),
    session: AsyncSession = Depends(session_dep),
):
    """Update the selected model's connection fields (admin only; name fixed).

    An empty ``api_key`` field KEEPS the stored key (the service only
    encrypts a non-empty value); clearing it is the explicit clear action.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    row = await model_service.update_model(session, row, {
        "base_url": base_url.strip() or None,
        "api_key_env": api_key_env.strip() or None,
        "api_model": api_model.strip() or None,
        "temperature": float(temperature) if temperature.strip() else None,
        "max_output_tokens": int(max_output_tokens) if max_output_tokens.strip() else None,
        "description": description.strip() or None,
        # Non-empty replaces the stored key; empty keeps it.
        "api_key": api_key.strip() or None,
    })
    await audit_service.record_event(
        session, user.id, "model_update", None,
        f"model={row.name} key={'replaced' if api_key.strip() else 'unchanged'}",
    )
    return RedirectResponse(url=f"/models?model_id={model_id}", status_code=302)


@panel_app.post("/models/{model_id}/test")
async def panel_model_test(request: Request, model_id: int, session: AsyncSession = Depends(session_dep)):
    """Probe one model with the shared 1-token helper; the key is never shown.

    Admin only: the probe spends a real provider call against a configured
    endpoint, so it is a configuration action, not a viewer capability.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    row = await model_service.get_model(session, model_id)
    if not row:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
    started = time.perf_counter()
    ok, error = await model_service.test_model_connection(row)
    latency_ms = int((time.perf_counter() - started) * 1000)
    # Persist the outcome on the row so the table's status column is real
    # recorded data (never tested / ok / failed + time + reason), not a claim.
    await model_service.record_test_result(session, row, ok=ok, error=error, latency_ms=latency_ms)
    test_result = {"ok": ok, "model": row.name, "error": error, "latency_ms": latency_ms}
    ok_msg = f"اتصال مدل {row.name} موفق بود ({latency_ms} میلی‌ثانیه)." if ok else None
    missing_key = error == "no API key configured"
    await audit_service.record_event(
        session, user.id, "model_test", None,
        f"model={row.name} ok={ok} latency_ms={latency_ms} error={(error or '')[:120]}",
    )
    rows = await model_service.list_models(session)
    selected = row
    _attach_creds(rows)
    counts_ctx = await dashboard_service.sidebar_badges(session)
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
    counts_ctx = await dashboard_service.sidebar_badges(session)
    return templates.TemplateResponse(request, "prompts.html",
        _ctx(request, user, active="prompts", **counts_ctx, prompts=rows, selected=selected))


@panel_app.post("/prompts")
async def panel_prompt_create(request: Request, version: str = Form(...), system_prompt: str = Form(...), session: AsyncSession = Depends(session_dep)):
    """Create a new prompt version as a new row (never edits in place)."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    try:
        row = await prompt_service.create_prompt(
            session, version=version.strip(), system_prompt=system_prompt, is_active=False
        )
    except ValueError:
        rows = await prompt_service.list_prompts(session)
        selected = next((p for p in rows if p.is_active), rows[0] if rows else None)
        counts_ctx = await dashboard_service.sidebar_badges(session)
        return templates.TemplateResponse(request, "prompts.html",
            _ctx(request, user, active="prompts", **counts_ctx, prompts=rows, selected=selected,
                 error="نسخه تکراری است."), status_code=422)
    await audit_service.record_event(session, user.id, "prompt_create", None, f"version={version}")
    return RedirectResponse(url="/prompts", status_code=302)


@panel_app.post("/prompts/{prompt_id}/activate")
async def panel_prompt_activate(request: Request, prompt_id: int, session: AsyncSession = Depends(session_dep)):
    """Activate one prompt version atomically (previous stays as history)."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_CONFIG)
    except PermissionDenied:
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
    counts_ctx = await dashboard_service.sidebar_badges(session)
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
    try:
        _require_role(user, Action.SUBMIT_RECORD)
    except PermissionDenied:
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
        counts_ctx = await dashboard_service.sidebar_badges(session)
        return templates.TemplateResponse(request, "submit.html",
            _ctx(request, user, active="submit", **counts_ctx, models=models, prompts=prompts,
                 error=message),
            status_code=422)
    return RedirectResponse(url=f"/records/{row.id}", status_code=302)


# --------------------------------------------------------------------------
# users & audit (admin only; viewer/editor get 403)
# --------------------------------------------------------------------------


def _require_admin_or_403(request: Request, user):
    """Kept for the users GET page; every other route calls ``_require_role``."""
    try:
        _require_role(user, Action.MANAGE_USERS)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    return None


#: Path -> the action it protects, for routes whose guard is reached through a
#: helper (so ``tests/test_authz_matrix.py`` can assert the mapping).
GUARDED_ROUTES: dict[str, str] = {
    "/users": "MANAGE_USERS",
    "/users/{user_id}/role": "MANAGE_USERS",
    "/users/{user_id}/toggle": "MANAGE_USERS",
    "/users/{user_id}/password": "MANAGE_USERS",
    "/users/{user_id}/delete": "MANAGE_USERS",
    "/audit": "VIEW_AUDIT",
    "/deliveries/{delivery_id}/retry": "RETRY_DELIVERY",
    "/deliveries/{delivery_id}/release": "RETRY_DELIVERY",
    "/deliveries/{delivery_id}/cancel": "RETRY_DELIVERY",
    "/deliveries/{delivery_id}/delete-from-channel": "RETRY_DELIVERY",
    "/deliveries/{delivery_id}/mark-deleted": "RETRY_DELIVERY",
    "/channels": "MANAGE_CONFIG",
    "/channels/{channel_id}": "MANAGE_CONFIG",
    "/channels/{channel_id}/toggle": "MANAGE_CONFIG",
    "/channels/{channel_id}/delete": "MANAGE_CONFIG",
    "/channels/category-channel": "MANAGE_CONFIG",
    "/fields/{field_id}/category": "FIELD_CURATE",
    "/submit": "SUBMIT_RECORD",
    "/records/{record_id}/regenerate": "REVIEW_RECORD",
    "/records/{record_id}/delete-everywhere": "RETRY_DELIVERY",
}

#: Routes whose guard is ``require_login`` rather than a role check: a signed-in
#: user may always manage their own account, whatever their role. They still
#: require the current password, which is the real check.
SELF_SERVICE_ROUTES: frozenset[str] = frozenset({
    "/profile/password",
})


@panel_app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request, user_id: int | None = None, session: AsyncSession = Depends(session_dep)
):
    """List console users with the role access matrix (admin only).

    ``user_id`` selects which account the single settings block is editing, so
    the page never grows a new pair of sections per user.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_USERS)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

    users = await user_service.list_users(session)
    roles = await user_service.list_roles(session)
    selected_user = None
    if user_id is not None:
        selected_user = next((u for u in users if u["id"] == user_id), None)
    if selected_user is None and users:
        # Default to the first account so the block is never a dead form.
        selected_user = users[0]
    counts_ctx = await dashboard_service.sidebar_badges(session)
    return templates.TemplateResponse(request, "users.html",
        _ctx(request, user, active="users", **counts_ctx, users=users, roles=roles,
             selected_user=selected_user))


@panel_app.post("/users")
async def user_create(
    request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...),
    session: AsyncSession = Depends(session_dep),
):
    """Create a console user with an argon2-hashed password (admin only)."""
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_USERS)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
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
    try:
        _require_role(user, Action.MANAGE_USERS)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
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
    try:
        _require_role(user, Action.MANAGE_USERS)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

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
    try:
        _require_role(user, Action.MANAGE_USERS)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

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
    counts_ctx = await dashboard_service.sidebar_badges(session)
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
    counts_ctx = await dashboard_service.sidebar_badges(session)
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

    Deactivation stays the available alternative (see the toggle button).
    Deletion tombstones the account: it leaves every panel listing and can no
    longer log in, while the audit trail keeps the person's name.
    """
    user = await require_login(request, session)
    try:
        _require_role(user, Action.MANAGE_USERS)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)

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
    try:
        _require_role(user, Action.VIEW_AUDIT)
    except PermissionDenied:
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
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
    counts_ctx = await dashboard_service.sidebar_badges(session)
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in {"action": action, "username": username, "main_id": main_id}.items() if v})
    return templates.TemplateResponse(request, "audit.html",
        _ctx(request, user, active="audit", **counts_ctx, events=events, total=total, page=page,
             page_size=page_size, action=action, username=username, main_id=main_id, query=query,
             users=users, actions=sorted(_ACTION_FA.items())))
