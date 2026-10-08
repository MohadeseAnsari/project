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


templates.env.filters["jalali"] = _jalali


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
    base = {"request": request, "current_user": user, "active": "", "error": None, "ok": None}
    base.update(extra)
    return base


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
    """Show record/delivery counts plus the newest records (no timings)."""
    user = await require_login(request)
    async with async_session() as session:
        record_rows = (await session.execute(select(Main.status, func.count()).group_by(Main.status))).all()
        delivery_rows = (await session.execute(select(Delivery.status, func.count()).group_by(Delivery.status))).all()
        heartbeat = (await session.execute(select(func.max(Main.heartbeat_at)))).scalar_one_or_none()
        recent = (
            await session.execute(select(Main).order_by(Main.id.desc()).limit(10))
        ).scalars().all()
    return templates.TemplateResponse(request, "dashboard.html",
        _ctx(
            request, user, active="dashboard",
            records=dict(record_rows), deliveries=dict(delivery_rows),
            heartbeat=heartbeat, recent=recent,
        ),
    )


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
    q: str = "", order: str = "newest",
):
    """Show the filterable record list (rows load via HTMX partial)."""
    user = await require_login(request)
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in {"status": status, "field": field, "university": university, "q": q, "order": order}.items() if v})
    return templates.TemplateResponse(request, "records.html",
        _ctx(request, user, active="records", status=status, field=field, university=university, q=q, order=order, query=query),
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
async def record_detail(request: Request, record_id: int):
    """Show one record with editable output, deliveries, and audit history."""
    user = await require_login(request)
    async with async_session() as session:
        record = await session.get(Main, record_id)
        if not record:
            return templates.TemplateResponse(request, "base.html", _ctx(request, user, error="رکورد یافت نشد."), status_code=404)
        deliveries = (
            await session.execute(select(Delivery).where(Delivery.main_id == record.id).order_by(Delivery.id))
        ).scalars().all()
        for d in deliveries:
            d.channel = await session.get(Channel, d.channel_id)
        history = (
            await session.execute(select(AuditLog).where(AuditLog.main_id == record.id).order_by(AuditLog.id.desc()).limit(50))
        ).scalars().all()
        for h in history:
            h.user = await session.get(PanelUser, h.user_id) if h.user_id else None
    return templates.TemplateResponse(request, "record_detail.html", _ctx(request, user, active="records", record=record, deliveries=deliveries, history=history)
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
    """Show the filterable delivery list (rows load via HTMX partial)."""
    user = await require_login(request)
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in {"status": status, "platform": platform, "main_id": main_id}.items() if v})
    return templates.TemplateResponse(request, "deliveries.html", _ctx(request, user, active="deliveries", status=status, platform=platform, main_id=main_id, query=query)
    )


@panel_app.get("/deliveries/table", response_class=HTMLResponse)
async def deliveries_table(request: Request, status: str = "", platform: str = "", main_id: str = "", page: int = 1, page_size: int = 20):
    """HTMX partial: one page of filtered deliveries (used by the list page)."""
    user = await require_login(request)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    async with async_session() as session:
        base = select(func.count()).select_from(Delivery)
        if platform:
            base = base.join(Channel, Channel.id == Delivery.channel_id).where(Channel.platform == platform)
        if status:
            base = base.where(Delivery.status == status)
        if main_id.strip().isdigit():
            base = base.where(Delivery.main_id == int(main_id.strip()))
        total = (await session.execute(base)).scalar_one()
        query = select(Delivery, Channel).join(Channel, Channel.id == Delivery.channel_id).order_by(Delivery.id.desc()).limit(page_size).offset((page - 1) * page_size)
        if status:
            query = query.where(Delivery.status == status)
        if platform:
            query = query.where(Channel.platform == platform)
        if main_id.strip().isdigit():
            query = query.where(Delivery.main_id == int(main_id.strip()))
        pairs = (await session.execute(query)).all()
        items = []
        for delivery, channel in pairs:
            delivery.channel = channel
            items.append(delivery)
    return templates.TemplateResponse(request, "deliveries_table.html",
        {"request": request, "current_user": user, "items": items, "total": total, "page": page},
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
    """List known thesis fields with their categories."""
    user = await require_login(request)
    async with async_session() as session:
        rows = (await session.execute(select(Field).order_by(Field.id.desc()).limit(200))).scalars().all()
        for f in rows:
            f.category = await session.get(Category, f.category_id) if f.category_id else None
    return templates.TemplateResponse(request, "fields.html", _ctx(request, user, active="fields", fields=rows))


@panel_app.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request):
    """List deterministic field categories."""
    user = await require_login(request)
    async with async_session() as session:
        rows = (await session.execute(select(Category).order_by(Category.id))).scalars().all()
    return templates.TemplateResponse(request, "categories.html", _ctx(request, user, active="categories", categories=rows))


@panel_app.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    """List channels and field→channel routes (admin manages them)."""
    user = await require_login(request)
    async with async_session() as session:
        channels = (await session.execute(select(Channel).order_by(Channel.id))).scalars().all()
        routes = (await session.execute(select(FieldChannelRoute).limit(500))).scalars().all()
        for r in routes:
            field = await session.get(Field, r.field_id)
            r.field_name = field.name if field else "—"
    return templates.TemplateResponse(request, "channels.html", _ctx(request, user, active="channels", channels=channels, routes=routes))


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
async def models_page(request: Request):
    """List model configurations with activate/test actions."""
    user = await require_login(request)
    async with async_session() as session:
        rows = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
    return templates.TemplateResponse(request, "models.html", _ctx(request, user, active="models", models=rows))


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
            return templates.TemplateResponse(request, "models.html", _ctx(request, user, active="models", models=[], error="نام مدل تکراری است."), status_code=422)
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
        if not api_key:
            models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
            return templates.TemplateResponse(request, "models.html", _ctx(request, user, active="models", models=models, error="کلید API پیکربندی نشده است."))
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=15.0)
            await client.chat.completions.create(model=row.name, messages=[{"role": "user", "content": "test"}], max_tokens=1)
            ok_msg = f"اتصال مدل {row.name} موفق بود."
            await audit(session, user.id, "model_test", None, f"model={row.name} ok=true")
        except Exception as e:
            models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
            return templates.TemplateResponse(request, "models.html", _ctx(request, user, active="models", models=models, error=f"اتصال ناموفق: {e}"[:500]))
        models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
    return templates.TemplateResponse(request, "models.html", _ctx(request, user, active="models", models=models, ok=ok_msg))


@panel_app.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    """List immutable prompt versions with an editor for the next version."""
    user = await require_login(request)
    async with async_session() as session:
        rows = (await session.execute(select(Prompt).order_by(Prompt.id))).scalars().all()
    return templates.TemplateResponse(request, "prompts.html", _ctx(request, user, active="prompts", prompts=rows))


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
            return templates.TemplateResponse(request, "prompts.html", _ctx(request, user, active="prompts", prompts=[], error="نسخه تکراری است."), status_code=422)
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
    return templates.TemplateResponse(request, "submit.html", _ctx(request, user, active="submit", models=models, prompts=prompts))


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
