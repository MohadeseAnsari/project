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
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.connection import async_session, engine, get_session
from app.db.models import (
    AuditLog,
    Category,
    Channel,
    Delivery,
    Field,
    FieldChannelRoute,
    Main,
    Model,
    PanelUser,
    Prompt,
    Role,
)
from app.messaging.dispatcher import confirm_deliveries_for_article
from app.panel.auth import audit, ensure_admin_user, session_user_id

logger = logging.getLogger(__name__)
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


templates.env.filters["jalali"] = _jalali
templates.env.filters["fa"] = _fa
templates.env.globals["role_fa"] = _role_fa


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the first admin from env, dispose the shared engine on shutdown."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await ensure_admin_user()
    yield
    await engine.dispose()


panel_app = FastAPI(title="IranDoc Review Panel", docs_url=None, redoc_url=None, lifespan=lifespan)
panel_app.mount("/static", StaticFiles(directory=str(_here / "static")), name="static")


async def current_user(request: Request) -> PanelUser | None:
    """Load the logged-in panel user (with role) or None."""
    uid = session_user_id(request)
    if uid is None:
        return None
    async with async_session() as session:
        user = await session.get(PanelUser, uid)
        if not user or not user.is_active:
            return None
        await session.refresh(user, attribute_names=["role"])
        # Detach values needed by templates after the session closes.
        role = await session.get(Role, user.role_id)
        user.role = role
        return user


async def require_login(request: Request) -> PanelUser:
    """Redirect anonymous browsers to the login page (panel is human-only)."""
    user = await current_user(request)
    if user is None:
        raise _LoginRedirect()
    return user


class _LoginRedirect(Exception):
    """Internal control flow: unauthenticated panel access goes to /login."""


@panel_app.exception_handler(_LoginRedirect)
async def _login_redirect_handler(request: Request, exc: _LoginRedirect):
    return RedirectResponse(url="/login", status_code=302)


def _ctx(request: Request, user: PanelUser | None, **extra):
    base = {
        "request": request, "current_user": user, "active": "",
        "error": None, "ok": None, "review_count": None, "gated_count": None,
    }
    base.update(extra)
    return base


async def _sidebar_counts(session: AsyncSession) -> dict:
    """Sidebar badges: records awaiting review + deliveries needing attention."""
    review_total = (
        await session.execute(select(func.count()).select_from(Main).where(Main.status == "review"))
    ).scalar_one()
    gated_total = (
        await session.execute(
            select(func.count()).select_from(Delivery).where(Delivery.status.in_(["failed", "awaiting_confirmation"])))
    ).scalar_one()
    return {"review_count": review_total or None, "gated_count": gated_total or None}


def _is_admin(user: PanelUser) -> bool:
    return bool(user.role and user.role.name == "admin")


# --------------------------------------------------------------------------
# auth pages
# --------------------------------------------------------------------------


@panel_app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show the Persian login form (public; the only anonymous page)."""
    user = await current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html", _ctx(request, None))


@panel_app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    """Verify credentials, sign the session cookie, and audit the login."""
    from app.panel.auth import hash_password, set_session_cookie, verify_password  # noqa: F401 (keeps import local, avoids cycles)

    async with async_session() as session:
        row = (
            await session.execute(select(PanelUser).where(PanelUser.username == username))
        ).scalar_one_or_none()
        if not row or not row.is_active or not verify_password(row.password_hash, password):
            return templates.TemplateResponse(request, "login.html", _ctx(request, None, error="نام کاربری یا گذرواژه نادرست است."), status_code=401
            )
        await audit(session, row.id, "login", None, f"username={username}")
        response = RedirectResponse(url="/", status_code=302)
        set_session_cookie(response, row.id)
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
async def dashboard(request: Request):
    """Show KPIs, a 7-day chart, health, recent events, and channel state."""
    from datetime import timedelta

    user = await require_login(request)
    async with async_session() as session:
        counts = dict(
            (await session.execute(select(Main.status, func.count()).group_by(Main.status))).all()
        )
        delivery_counts = dict(
            (await session.execute(select(Delivery.status, func.count()).group_by(Delivery.status))).all()
        )
        kpi_review = counts.get("review", 0)
        kpi_queued = counts.get("ready", 0) + counts.get("processing", 0)
        kpi_sent = delivery_counts.get("sent", 0)
        kpi_failed = counts.get("failed", 0) + delivery_counts.get("failed", 0)

        # Real 7-day series: records created per day, heights normalized.
        today = datetime.utcnow().date()
        days = [today - timedelta(days=i) for i in range(6, -1, -1)]
        per_day = []
        peak = 0
        for day in days:
            start = datetime(day.year, day.month, day.day)
            end = start + timedelta(days=1)
            n = (
                await session.execute(
                    select(func.count()).select_from(Main).where(Main.created_at >= start, Main.created_at < end))
            ).scalar_one()
            per_day.append((day, n))
            peak = max(peak, n)
        chart = [
            {
                "label": jdatetime.date.fromgregorian(date=d).strftime("%A"),
                "pct": int(round(n / peak * 100)) if peak else 0,
            }
            for d, n in per_day
        ]

        heartbeat = (await session.execute(select(func.max(Main.heartbeat_at)))).scalar_one_or_none()
        worker_fresh = bool(
            heartbeat and (datetime.utcnow() - heartbeat).total_seconds() < settings.worker_lease_minutes * 60
        )
        pending_deliveries = delivery_counts.get("pending", 0)

        # Timeline: latest human/system events from the audit trail.
        audits = (
            await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(4))
        ).scalars().all()
        timeline = []
        for a in audits:
            actor = None
            if a.user_id:
                actor = await session.get(PanelUser, a.user_id)
            timeline.append(
                {
                    "icon": {"approve": "✓", "reject": "!", "retry": "➤", "login": "◕"}.get(a.action, "•"),
                    "color": {"approve": "green", "reject": "red", "retry": "amber", "login": "violet"}.get(a.action, "blue"),
                    "title": _audit_title(a),
                    "sub": f"{actor.username if actor else 'سیستم'} · {_jalali(a.created_at)}",
                }
            )

        channels = (await session.execute(select(Channel).order_by(Channel.id))).scalars().all()
        for c in channels:
            c.route_count = (
                await session.execute(
                    select(func.count()).select_from(FieldChannelRoute).where(FieldChannelRoute.channel_id == c.id))
            ).scalar_one()

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
    "login": "ورود به کنسول", "approve": "تأیید و انتشار", "reject": "رد خبر",
    "save_draft": "ذخیرهٔ پیش‌نویس", "retry": "تلاش مجدد ارسال",
    "submit": "ثبت رکورد", "model_create": "ساخت مدل", "model_activate": "فعال‌سازی مدل",
    "model_test": "آزمون اتصال مدل", "prompt_create": "ساخت پرامپت",
    "prompt_activate": "فعال‌سازی پرامپت", "channel_create": "ساخت کانال",
    "route": "افزودن مسیر", "unroute": "حذف مسیر",
}


def _audit_title(entry: AuditLog) -> str:
    """Persian one-line summary of an audit entry for timelines and tables."""
    action = _ACTION_FA.get(entry.action, entry.action)
    if entry.main_id:
        return f"{action} — رکورد {entry.main_id}"
    if entry.detail:
        return f"{action} — {entry.detail}"
    return action


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


def _record_filters(status, field, university, q):
    filters = []
    if status:
        filters.append(Main.status == status)
    if field:
        filters.append(Main.field.ilike(f"%{field}%"))
    if university:
        filters.append(Main.university.ilike(f"%{university}%"))
    if q:
        like = f"%{q}%"
        filters.append(
            or_(
                Main.title.ilike(like), Main.abstract.ilike(like), Main.field.ilike(like),
                Main.university.ilike(like), Main.author.ilike(like), Main.supervisor.ilike(like),
            )
        )
    return filters


@panel_app.get("/records", response_class=HTMLResponse)
async def records_page(
    request: Request, status: str = "", field: str = "", university: str = "",
    q: str = "", order: str = "newest", page: int = 1,
):
    """Show the filterable record list (first page server-rendered, rest via HTMX)."""
    user = await require_login(request)
    from urllib.parse import urlencode

    page = max(1, page)
    page_size = 20
    async with async_session() as session:
        counts = dict(
            (await session.execute(select(Main.status, func.count()).group_by(Main.status))).all()
        )
        delivery_counts = dict(
            (await session.execute(select(Delivery.status, func.count()).group_by(Delivery.status))).all()
        )
        field_options = [
            r[0] for r in (
                await session.execute(select(Main.field).distinct().order_by(Main.field).limit(50))
            ).all()
        ]
        university_options = [
            r[0] for r in (
                await session.execute(select(Main.university).distinct().order_by(Main.university).limit(50))
            ).all()
        ]
        filters = _record_filters(status or None, field or None, university or None, q or None)
        count_q = select(func.count()).select_from(Main)
        if filters:
            count_q = count_q.where(*filters)
        total = (await session.execute(count_q)).scalar_one()
        ordering = [Main.created_at.desc(), Main.id.desc()] if order != "oldest" else [Main.created_at.asc(), Main.id.asc()]
        query_sel = select(Main)
        if filters:
            query_sel = query_sel.where(*filters)
        items = (
            await session.execute(query_sel.order_by(*ordering).limit(page_size).offset((page - 1) * page_size))
        ).scalars().all()
        counts_ctx = await _sidebar_counts(session)
    query = urlencode({k: v for k, v in {"status": status, "field": field, "university": university, "q": q, "order": order}.items() if v})
    return templates.TemplateResponse(request, "records.html",
        _ctx(
            request, user, active="records", **counts_ctx,
            status=status, field=field, university=university, q=q, order=order, query=query,
            kpi_review=counts.get("review", 0),
            kpi_queued=counts.get("ready", 0) + counts.get("processing", 0),
            kpi_sent=delivery_counts.get("sent", 0),
            kpi_failed=counts.get("failed", 0) + delivery_counts.get("failed", 0),
            field_options=field_options, university_options=university_options,
            items=items, total=total, page=page, page_size=page_size,
        ),
    )


@panel_app.get("/records/table", response_class=HTMLResponse)
async def records_table(
    request: Request, status: str = "", field: str = "", university: str = "",
    q: str = "", order: str = "newest", page: int = 1, page_size: int = 20,
):
    """HTMX partial: one page of filtered record rows (used by the list page)."""
    user = await require_login(request)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    filters = _record_filters(status or None, field or None, university or None, q or None)
    async with async_session() as session:
        count_q = select(func.count()).select_from(Main)
        if filters:
            count_q = count_q.where(*filters)
        total = (await session.execute(count_q)).scalar_one()
        ordering = [Main.created_at.desc(), Main.id.desc()] if order != "oldest" else [Main.created_at.asc(), Main.id.asc()]
        query = select(Main)
        if filters:
            query = query.where(*filters)
        rows = (
            await session.execute(query.order_by(*ordering).limit(page_size).offset((page - 1) * page_size))
        ).scalars().all()
    from urllib.parse import urlencode

    query_str = urlencode({k: v for k, v in {"status": status, "field": field, "university": university, "q": q, "order": order}.items() if v})
    return templates.TemplateResponse(request, "records_table.html",
        {"request": request, "current_user": user, "items": rows, "total": total, "page": page, "page_size": page_size, "query": query_str},
    )


@panel_app.get("/records/{record_id}", response_class=HTMLResponse)
async def record_detail(request: Request, record_id: int, tab: str = "text"):
    """Show one record with editable output, raw/meta/history/delivery tabs."""
    user = await require_login(request)
    if tab not in ("text", "raw", "meta", "history", "delivery"):
        tab = "text"
    async with async_session() as session:
        record = await session.get(Main, record_id)
        if not record:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رکورد یافت نشد."), status_code=404)
        deliveries = (
            await session.execute(select(Delivery).where(Delivery.main_id == record.id).order_by(Delivery.id))
        ).scalars().all()
        for d in deliveries:
            d.channel = await session.get(Channel, d.channel_id)
        model_name = None
        if record.model_id:
            model = await session.get(Model, record.model_id)
            model_name = model.name if model else None
        prompt_version = None
        if record.prompt_id:
            prompt = await session.get(Prompt, record.prompt_id)
            prompt_version = prompt.version if prompt else None
        routed_channels = None
        if record.field_id:
            routed = (
                await session.execute(
                    select(Channel).join(FieldChannelRoute, FieldChannelRoute.channel_id == Channel.id).where(
                        FieldChannelRoute.field_id == record.field_id))
            ).scalars().all()
            if routed:
                routed_channels = "، ".join(c.username or c.title or c.platform for c in routed)
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
        elif record.status == "rejected":
            history.append({"title": "خبر رد شد", "sub": f"{_jalali(record.updated_at)}"})
        audits = (
            await session.execute(select(AuditLog).where(AuditLog.main_id == record.id).order_by(AuditLog.id.desc()).limit(20))
        ).scalars().all()
        for a in audits:
            actor = await session.get(PanelUser, a.user_id) if a.user_id else None
            history.append({"title": _audit_title(a), "sub": f"{actor.username if actor else 'سیستم'} · {_jalali(a.created_at)}"})
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "record_detail.html",
        _ctx(
            request, user, active="records", **counts_ctx, record=record, deliveries=deliveries,
            history=history, tab=tab, model_name=model_name, prompt_version=prompt_version,
            routed_channels=routed_channels,
        )
    )


@panel_app.post("/records/{record_id}/review")
async def record_review(
    request: Request, record_id: int,
    action: str = Form(...), title: str = Form(""), lead: str = Form(""), body: str = Form(""),
):
    """Apply approve/reject/save_draft from the review form and audit it."""
    user = await require_login(request)
    if action not in ("approve", "reject", "save_draft"):
        return RedirectResponse(url=f"/records/{record_id}", status_code=302)
    async with async_session() as session:
        record = await session.get(Main, record_id)
        if not record or record.status != "review":
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رکورد در وضعیت بررسی نیست."), status_code=409
            )
        current = dict(record.output_json or {})
        if title.strip():
            current["title"] = title.strip()
        if lead.strip():
            current["lead"] = lead.strip()
        if body.strip():
            current["body"] = body.strip()
        record.output_json = current
        if action == "approve":
            await session.commit()
            await confirm_deliveries_for_article(session, record.id)
            await audit(session, user.id, "approve", record.id, "تأیید و انتشار")
        elif action == "reject":
            record.status = "rejected"
            await session.commit()
            await audit(session, user.id, "reject", record.id, "رد رکورد")
        else:
            await session.commit()
            await audit(session, user.id, "save_draft", record.id, "ذخیره پیش‌نویس")
    return RedirectResponse(url=f"/records/{record_id}", status_code=302)


# --------------------------------------------------------------------------
# deliveries
# --------------------------------------------------------------------------


@panel_app.get("/deliveries", response_class=HTMLResponse)
async def deliveries_page(request: Request, status: str = "", platform: str = "", main_id: str = ""):
    """Show the filterable delivery list (first page server-rendered)."""
    user = await require_login(request)
    from urllib.parse import urlencode

    page_size = 20
    status_f = status or None
    platform_f = platform or None
    mid = int(main_id.strip()) if main_id.strip().isdigit() else None
    async with async_session() as session:
        base = select(func.count()).select_from(Delivery)
        if platform_f:
            base = base.join(Channel, Channel.id == Delivery.channel_id).where(Channel.platform == platform_f)
        if status_f:
            base = base.where(Delivery.status == status_f)
        if mid is not None:
            base = base.where(Delivery.main_id == mid)
        total = (await session.execute(base)).scalar_one()
        query = select(Delivery, Channel, Main).join(
            Channel, Channel.id == Delivery.channel_id).join(Main, Main.id == Delivery.main_id)
        if status_f:
            query = query.where(Delivery.status == status_f)
        if platform_f:
            query = query.where(Channel.platform == platform_f)
        if mid is not None:
            query = query.where(Delivery.main_id == mid)
        pairs = (await session.execute(query.order_by(Delivery.id.desc()).limit(page_size))).all()
        items = [
            {
                "id": d.id, "main_id": d.main_id, "status": d.status, "attempts": d.attempts,
                "platform_message_id": d.platform_message_id, "title": m.title, "field": m.field,
                "channel_name": c.username or c.title or c.platform,
            }
            for d, c, m in pairs
        ]
        last_failed = (
            await session.execute(select(Delivery).where(Delivery.status == "failed").order_by(Delivery.id.desc()).limit(1))
        ).scalar_one_or_none()
        last_error = f"خطای آخر: «{last_failed.error_message}» — {_jalali(last_failed.updated_at)}" if last_failed and last_failed.error_message else None
        counts_ctx = await _sidebar_counts(session)
    query_str = urlencode({k: v for k, v in {"status": status, "platform": platform, "main_id": main_id}.items() if v})
    return templates.TemplateResponse(request, "deliveries.html",
        _ctx(
            request, user, active="deliveries", **counts_ctx, status=status, platform=platform,
            main_id=main_id, query=query_str, items=items, total=total, page=1, page_size=page_size,
            last_error=last_error, max_attempts=settings.sender_max_attempts,
            backoff_base=settings.sender_backoff_base_seconds,
        )
    )


@panel_app.get("/deliveries/table", response_class=HTMLResponse)
async def deliveries_table(request: Request, status: str = "", platform: str = "", main_id: str = "", page: int = 1, page_size: int = 20):
    """HTMX partial: one page of filtered deliveries (used by the list page)."""
    user = await require_login(request)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    status_f = status or None
    platform_f = platform or None
    mid = int(main_id.strip()) if main_id.strip().isdigit() else None
    async with async_session() as session:
        base = select(func.count()).select_from(Delivery)
        if platform_f:
            base = base.join(Channel, Channel.id == Delivery.channel_id).where(Channel.platform == platform_f)
        if status_f:
            base = base.where(Delivery.status == status_f)
        if mid is not None:
            base = base.where(Delivery.main_id == mid)
        total = (await session.execute(base)).scalar_one()
        query = select(Delivery, Channel, Main).join(
            Channel, Channel.id == Delivery.channel_id).join(Main, Main.id == Delivery.main_id)
        if status_f:
            query = query.where(Delivery.status == status_f)
        if platform_f:
            query = query.where(Channel.platform == platform_f)
        if mid is not None:
            query = query.where(Delivery.main_id == mid)
        pairs = (await session.execute(query.order_by(Delivery.id.desc()).limit(page_size).offset((page - 1) * page_size))).all()
        items = [
            {
                "id": d.id, "main_id": d.main_id, "status": d.status, "attempts": d.attempts,
                "platform_message_id": d.platform_message_id, "title": m.title, "field": m.field,
                "channel_name": c.username or c.title or c.platform,
            }
            for d, c, m in pairs
        ]
    from urllib.parse import urlencode

    query_str = urlencode({k: v for k, v in {"status": status, "platform": platform, "main_id": main_id}.items() if v})
    return templates.TemplateResponse(request, "deliveries_table.html",
        {"request": request, "current_user": user, "items": items, "total": total, "page": page, "page_size": page_size, "query": query_str},
    )


@panel_app.post("/deliveries/{delivery_id}/retry")
async def delivery_retry(request: Request, delivery_id: int):
    """Re-queue a failed delivery (attempts reset) and audit the retry."""
    user = await require_login(request)
    async with async_session() as session:
        delivery = await session.get(Delivery, delivery_id)
        if not delivery or delivery.status != "failed":
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="ارسال قابل تلاش مجدد نیست."), status_code=409)
        delivery.status = "pending"
        delivery.attempts = 0
        delivery.claimed_at = None
        delivery.error_message = None
        delivery.next_attempt_at = datetime.utcnow()
        delivery.updated_at = datetime.utcnow()
        await session.commit()
        await audit(session, user.id, "retry", delivery.main_id, f"delivery={delivery.id}")
    return RedirectResponse(url="/deliveries", status_code=302)


# --------------------------------------------------------------------------
# reference data
# --------------------------------------------------------------------------


@panel_app.get("/fields", response_class=HTMLResponse)
async def fields_page(request: Request):
    """List known thesis fields with categories, routes, and top-field bars."""
    user = await require_login(request)
    async with async_session() as session:
        rows = (await session.execute(select(Field).order_by(Field.id.desc()).limit(100))).scalars().all()
        fields = []
        for f in rows:
            cat = await session.get(Category, f.category_id) if f.category_id else None
            routed = (
                await session.execute(
                    select(Channel).join(FieldChannelRoute, FieldChannelRoute.channel_id == Channel.id).where(
                        FieldChannelRoute.field_id == f.id))
            ).scalars().all()
            fields.append(
                {
                    "id": f.id, "name": f.name, "source": f.source,
                    "category_name": cat.name if cat else None,
                    "channel_names": "، ".join(c.username or c.title or c.platform for c in routed) or None,
                }
            )
        total = (await session.execute(select(func.count()).select_from(Field))).scalar_one()
        top_rows = (
            await session.execute(
                select(Main.field, func.count()).group_by(Main.field).order_by(func.count().desc()).limit(3))
        ).all()
        top_total = sum(n for _, n in top_rows) or 1
        top = [{"name": name, "pct": int(round(n / top_total * 100))} for name, n in top_rows]
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "fields.html",
        _ctx(request, user, active="fields", **counts_ctx, fields=fields, total=total, top=top))


@panel_app.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request, q: str = ""):
    """List categories as cards with their real field names as chips."""
    user = await require_login(request)
    async with async_session() as session:
        query = select(Category).order_by(Category.id)
        if q.strip():
            query = query.where(Category.name.ilike(f"%{q.strip()}%"))
        rows = (await session.execute(query)).scalars().all()
        total = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
        categories = []
        for c in rows:
            names = (
                await session.execute(select(Field.name).where(Field.category_id == c.id).order_by(Field.id).limit(9))
            ).scalars().all()
            n = (
                await session.execute(select(func.count()).select_from(Field).where(Field.category_id == c.id))
            ).scalar_one()
            categories.append(
                {"name": c.name, "description": c.description, "field_names": names, "field_count": n}
            )
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "categories.html",
        _ctx(request, user, active="categories", **counts_ctx, categories=categories, total=total, q=q))


@panel_app.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request, channel_id: int | None = None, new: str = ""):
    """List channels with route matrix and the selected channel settings."""
    user = await require_login(request)
    async with async_session() as session:
        channels = (await session.execute(select(Channel).order_by(Channel.id))).scalars().all()
        for c in channels:
            c.route_count = (
                await session.execute(
                    select(func.count()).select_from(FieldChannelRoute).where(FieldChannelRoute.channel_id == c.id))
            ).scalar_one()
        selected = None
        if channel_id is not None:
            selected = await session.get(Channel, channel_id)
        if selected is None:
            selected = channels[0] if channels else None
        fields = (await session.execute(select(Field).order_by(Field.id).limit(30))).scalars().all()
        matrix = []
        for f in fields:
            ids = set(
                (await session.execute(
                    select(FieldChannelRoute.channel_id).where(FieldChannelRoute.field_id == f.id))
                ).scalars().all()
            )
            matrix.append({"field_name": f.name, "channel_ids": ids})
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "channels.html",
        _ctx(request, user, active="channels", **counts_ctx, channels=channels, selected=selected,
             matrix=matrix, show_new=bool(new)))


@panel_app.post("/channels/{channel_id}/toggle")
async def channel_toggle(request: Request, channel_id: int):
    """Flip a channel's active switch (admin only)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        row = await session.get(Channel, channel_id)
        if not row:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کانال یافت نشد."), status_code=404)
        row.is_active = not row.is_active
        await session.commit()
        await audit(session, user.id, "channel_create", None, f"toggle channel={channel_id} active={row.is_active}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.post("/channels/{channel_id}")
async def channel_update(
    request: Request, channel_id: int, title: str = Form(""),
    chat_id: str = Form(""), username: str = Form(""),
):
    """Update a channel's display fields (admin only; no tokens on rows)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        row = await session.get(Channel, channel_id)
        if not row:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="کانال یافت نشد."), status_code=404)
        row.title = title.strip() or None
        row.username = username.strip() or None
        if chat_id.strip().lstrip("-").isdigit():
            row.chat_id = int(chat_id.strip())
        await session.commit()
        await audit(session, user.id, "channel_create", None, f"update channel={channel_id}")
    return RedirectResponse(url=f"/channels?channel_id={channel_id}", status_code=302)


@panel_app.post("/channels")
async def channel_create(
    request: Request, platform: str = Form(...), chat_id: int = Form(...),
    username: str = Form(""), title: str = Form(""),
):
    """Create a channel destination (admin only; tokens stay in env)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        session.add(Channel(platform=platform, chat_id=chat_id, username=username or None, title=title or None, is_active=True))
        await session.commit()
        await audit(session, user.id, "channel_create", None, f"{platform}:{chat_id}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.post("/channels/route")
async def channel_route(request: Request, field_id: int = Form(...), channel_id: int = Form(...)):
    """Route one field to one channel (admin only; row insert, no code change)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        session.add(FieldChannelRoute(field_id=field_id, channel_id=channel_id))
        try:
            await session.commit()
        except Exception:
            await session.rollback()
        await audit(session, user.id, "route", None, f"field={field_id} channel={channel_id}")
    return RedirectResponse(url="/channels", status_code=302)


@panel_app.post("/channels/unroute")
async def channel_unroute(request: Request, field_id: int = Form(...), channel_id: int = Form(...)):
    """Remove one field→channel route (admin only)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        row = await session.get(FieldChannelRoute, (field_id, channel_id))
        if row:
            await session.delete(row)
            await session.commit()
        await audit(session, user.id, "unroute", None, f"field={field_id} channel={channel_id}")
    return RedirectResponse(url="/channels", status_code=302)


# --------------------------------------------------------------------------
# models & prompts (admin only for mutations; immutable prompt versions)
# --------------------------------------------------------------------------


@panel_app.get("/models", response_class=HTMLResponse)
async def models_page(request: Request, model_id: int | None = None):
    """List models with the selected (default active) settings and test cards."""
    user = await require_login(request)
    async with async_session() as session:
        rows = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
        selected = None
        if model_id is not None:
            selected = await session.get(Model, model_id)
        if selected is None:
            selected = next((m for m in rows if m.is_active), rows[0] if rows else None)
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "models.html",
        _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected, test_result=None))


@panel_app.post("/models")
async def panel_model_create(
    request: Request, name: str = Form(...), provider: str = Form(...), version: str = Form(...),
    base_url: str = Form(""), api_key_env: str = Form(""), temperature: str = Form(""),
    max_output_tokens: str = Form(""), description: str = Form(""),
):
    """Create a model row from the panel form (admin only)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        session.add(
            Model(
                name=name.strip(), provider=provider.strip(), version=version.strip(),
                base_url=base_url.strip() or None, api_key_env=api_key_env.strip() or None,
                temperature=float(temperature) if temperature.strip() else None,
                max_output_tokens=int(max_output_tokens) if max_output_tokens.strip() else None,
                description=description.strip() or None, is_active=False,
            )
        )
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            rows = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
            selected = next((m for m in rows if m.is_active), rows[0] if rows else None)
            counts_ctx = await _sidebar_counts(session)
            return templates.TemplateResponse(request, "models.html",
                _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected,
                     test_result=None, error="نام مدل تکراری است."), status_code=422)
        await audit(session, user.id, "model_create", None, f"name={name}")
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}/activate")
async def panel_model_activate(request: Request, model_id: int):
    """Activate one model atomically (exactly one active row)."""
    from sqlalchemy import update

    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        row = await session.get(Model, model_id)
        if not row:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
        await session.execute(update(Model).where(Model.id != row.id).values(is_active=False))
        row.is_active = True
        await session.commit()
        await audit(session, user.id, "model_activate", None, f"model={row.name}")
    return RedirectResponse(url="/models", status_code=302)


@panel_app.post("/models/{model_id}")
async def panel_model_update(
    request: Request, model_id: int, base_url: str = Form(""),
    api_key_env: str = Form(""), temperature: str = Form(""),
    max_output_tokens: str = Form(""), description: str = Form(""),
):
    """Update the selected model's connection fields (admin only; name fixed)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        row = await session.get(Model, model_id)
        if not row:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
        row.base_url = base_url.strip() or None
        row.api_key_env = api_key_env.strip() or None
        row.temperature = float(temperature) if temperature.strip() else None
        row.max_output_tokens = int(max_output_tokens) if max_output_tokens.strip() else None
        row.description = description.strip() or None
        await session.commit()
        await audit(session, user.id, "model_create", None, f"update model={row.name}")
    return RedirectResponse(url=f"/models?model_id={model_id}", status_code=302)


@panel_app.post("/models/{model_id}/test")
async def panel_model_test(request: Request, model_id: int):
    """Probe one model with a real 1-token call; the key is never shown."""
    import os

    user = await require_login(request)
    async with async_session() as session:
        row = await session.get(Model, model_id)
        if not row:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="مدل یافت نشد."), status_code=404)
        base_url = row.base_url or settings.avalai_base_url
        api_key = (os.environ.get(row.api_key_env) if row.api_key_env else None) or settings.avalai_api_key
        test_result = None
        error = None
        ok_msg = None
        if not api_key:
            error = "کلید API پیکربندی نشده است."
        else:
            try:
                from openai import AsyncOpenAI

                client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=15.0)
                await client.chat.completions.create(model=row.name, messages=[{"role": "user", "content": "test"}], max_tokens=1)
                test_result = {"ok": True, "model": row.name, "error": None}
                ok_msg = f"اتصال مدل {row.name} موفق بود."
                await audit(session, user.id, "model_test", None, f"model={row.name} ok=true")
            except Exception as e:
                test_result = {"ok": False, "model": row.name, "error": str(e)[:500]}
                await audit(session, user.id, "model_test", None, f"model={row.name} ok=false")
        rows = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
        selected = await session.get(Model, model_id)
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "models.html",
        _ctx(request, user, active="models", **counts_ctx, models=rows, selected=selected,
             test_result=test_result, error=error, ok=ok_msg))


@panel_app.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    """Show the active prompt editor, versions, and the JSON contract."""
    user = await require_login(request)
    async with async_session() as session:
        rows = (await session.execute(select(Prompt).order_by(Prompt.id))).scalars().all()
        selected = next((p for p in rows if p.is_active), rows[0] if rows else None)
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "prompts.html",
        _ctx(request, user, active="prompts", **counts_ctx, prompts=rows, selected=selected))


@panel_app.post("/prompts")
async def panel_prompt_create(request: Request, version: str = Form(...), system_prompt: str = Form(...)):
    """Create a new prompt version as a new row (never edits in place)."""
    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        session.add(Prompt(version=version.strip(), system_prompt=system_prompt, is_active=False))
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            rows = (await session.execute(select(Prompt).order_by(Prompt.id))).scalars().all()
            selected = next((p for p in rows if p.is_active), rows[0] if rows else None)
            counts_ctx = await _sidebar_counts(session)
            return templates.TemplateResponse(request, "prompts.html",
                _ctx(request, user, active="prompts", **counts_ctx, prompts=rows, selected=selected,
                     error="نسخه تکراری است."), status_code=422)
        await audit(session, user.id, "prompt_create", None, f"version={version}")
    return RedirectResponse(url="/prompts", status_code=302)


@panel_app.post("/prompts/{prompt_id}/activate")
async def panel_prompt_activate(request: Request, prompt_id: int):
    """Activate one prompt version atomically (previous stays as history)."""
    from sqlalchemy import update

    user = await require_login(request)
    if not _is_admin(user):
        return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="دسترسی مدیریتی لازم است."), status_code=403)
    async with async_session() as session:
        row = await session.get(Prompt, prompt_id)
        if not row:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="پرامپت یافت نشد."), status_code=404)
        await session.execute(update(Prompt).where(Prompt.id != row.id).values(is_active=False))
        row.is_active = True
        await session.commit()
        await audit(session, user.id, "prompt_activate", None, f"version={row.version}")
    return RedirectResponse(url="/prompts", status_code=302)


# --------------------------------------------------------------------------
# panel submit
# --------------------------------------------------------------------------


@panel_app.get("/submit", response_class=HTMLResponse)
async def submit_page(request: Request):
    """Show the thesis submit form with model/prompt override selects."""
    user = await require_login(request)
    async with async_session() as session:
        models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
        prompts = (await session.execute(select(Prompt).order_by(Prompt.id))).scalars().all()
        counts_ctx = await _sidebar_counts(session)
    return templates.TemplateResponse(request, "submit.html",
        _ctx(request, user, active="submit", **counts_ctx, models=models, prompts=prompts))


@panel_app.post("/submit")
async def submit_create(
    request: Request, username: str = Form(...), title: str = Form(...), abstract: str = Form(...),
    field: str = Form(...), university: str = Form(...), table_of_contents: str = Form(...),
    author: str = Form(...), supervisor: str = Form(...), model_id: str = Form(""), prompt_id: str = Form(""),
):
    """Create a thesis job from the panel form (overrides validated, stored)."""
    user = await require_login(request)
    async with async_session() as session:
        mid = int(model_id) if model_id.strip().isdigit() else None
        pid = int(prompt_id) if prompt_id.strip().isdigit() else None
        if mid is not None and not await session.get(Model, mid):
            models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
            prompts = (await session.execute(select(Prompt).order_by(Prompt.id))).scalars().all()
            return templates.TemplateResponse(request, "submit.html", _ctx(request, user, active="submit", models=models, prompts=prompts, error="مدل انتخاب‌شده وجود ندارد."), status_code=422)
        if pid is not None and not await session.get(Prompt, pid):
            models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
            prompts = (await session.execute(select(Prompt).order_by(Prompt.id))).scalars().all()
            return templates.TemplateResponse(request, "submit.html", _ctx(request, user, active="submit", models=models, prompts=prompts, error="پرامپت انتخاب‌شده وجود ندارد."), status_code=422)
        row = Main(
            username=username.strip(), title=title.strip(), abstract=abstract.strip(), field=field.strip(),
            university=university.strip(), table_of_contents=table_of_contents.strip(),
            author=author.strip(), supervisor=supervisor.strip(),
            status="ready", model_id=mid, prompt_id=pid,
        )
        session.add(row)
        await session.commit()
        await audit(session, user.id, "submit", row.id, f"field={field}")
    return RedirectResponse(url=f"/records/{row.id}", status_code=302)
