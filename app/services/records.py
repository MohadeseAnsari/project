"""Record domain service: listing, detail lookup, and review transitions.

Single implementation shared by the machine API (``PATCH /records/{id}``)
and the panel (review form, bulk approve). Raises plain ``ValueError`` with
stable messages; routes map them to HTTP statuses or error pages.
"""

from datetime import datetime

from sqlalchemy import func, or_, select

from app.db.models import AuditLog, Channel, Delivery, Main, Model, PanelUser, Prompt
from app.messaging.dispatcher import confirm_deliveries_for_article
from app.services.transitions import assert_record_transition


async def create_thesis_record(
    session,
    *,
    username: str,
    title: str,
    abstract: str,
    field: str,
    university: str,
    table_of_contents: str | None,
    author: str,
    supervisor: str,
    model_id: int | None = None,
    prompt_id: int | None = None,
) -> Main:
    """Insert a ``ready`` thesis job with validated per-record overrides.

    The single intake flow for ``POST /submit`` and the panel submit form:
    both must produce exactly the row the generation Worker polls for.

    Raises:
        ValueError: ``model_id``/``prompt_id`` names a row that does not exist.
            Callers map this to their own 422/error surface (API JSON vs panel
            page) without changing the check itself.
    """
    if model_id is not None and await session.get(Model, model_id) is None:
        raise ValueError("model_id does not exist")
    if prompt_id is not None and await session.get(Prompt, prompt_id) is None:
        raise ValueError("prompt_id does not exist")
    record = Main(
        username=username,
        title=title,
        abstract=abstract,
        field=field,
        university=university,
        table_of_contents=table_of_contents,
        author=author,
        supervisor=supervisor,
        status="ready",
        model_id=model_id,
        prompt_id=prompt_id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


def record_filters(
    status: str | None = None,
    field: str | None = None,
    university: str | None = None,
    q: str | None = None,
    approved: bool | None = None,
) -> list:
    """Build the shared WHERE clauses for record listings (panel + API).

    ``approved`` is now status-based (the lifecycle owns the truth):
    True -> statuses ``approved``/``published``; False -> everything else
    (still undecided or failed/rejected).
    """
    filters = []
    if status:
        filters.append(Main.status == status)
    if approved is True:
        filters.append(Main.status.in_(("approved", "published")))
    elif approved is False:
        filters.append(Main.status.notin_(("approved", "published")))
    if field:
        filters.append(Main.field.ilike(f"%{field}%"))
    if university:
        filters.append(Main.university.ilike(f"%{university}%"))
    if q:
        like = f"%{q}%"
        filters.append(
            or_(
                Main.title.ilike(like),
                Main.abstract.ilike(like),
                Main.field.ilike(like),
                Main.university.ilike(like),
                Main.author.ilike(like),
                Main.supervisor.ilike(like),
            )
        )
    return filters


def record_ordering(order: str):
    """Newest-first (default) or oldest-first creation order for listings."""
    if order != "oldest":
        return [Main.created_at.desc(), Main.id.desc()]
    return [Main.created_at.asc(), Main.id.asc()]


async def list_records(
    session,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    field: str | None = None,
    university: str | None = None,
    q: str | None = None,
    order: str = "newest",
    approved: bool | None = None,
) -> tuple[list[Main], int]:
    """Return ``(page rows, filtered total)`` with SQL LIMIT/OFFSET + COUNT."""
    filters = record_filters(status, field, university, q, approved)
    count_query = select(func.count()).select_from(Main)
    if filters:
        count_query = count_query.where(*filters)
    total = (await session.execute(count_query)).scalar_one()
    query = select(Main)
    if filters:
        query = query.where(*filters)
    rows = (
        await session.execute(
            query.order_by(*record_ordering(order))
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).scalars().all()
    return list(rows), total


async def get_record(session, record_id: int) -> Main | None:
    """Fetch one record by id (both layers 404 the same way)."""
    result = await session.execute(select(Main).where(Main.id == record_id))
    return result.scalar_one_or_none()


async def record_detail(session, record: Main) -> dict:
    """Build the extended record payload (API GET/PATCH share this)."""
    from app.db.models import Channel, Delivery

    deliveries_result = await session.execute(
        select(Delivery, Channel)
        .join(Channel, Channel.id == Delivery.channel_id)
        .where(Delivery.main_id == record.id)
        .order_by(Delivery.id)
    )
    return {
        "id": record.id,
        "status": record.status,
        "field": record.field,
        "output_json": record.output_json,
        "error_message": record.error_message,
        "author": record.author,
        "supervisor": record.supervisor,
        "university": record.university,
        "table_of_contents": record.table_of_contents,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "approved_at": record.approved_at,
        "published_at": record.published_at,
        "field_id": record.field_id,
        "category_id": record.category_id,
        "model_id": record.model_id,
        "prompt_id": record.prompt_id,
        "deliveries": [
            {
                "id": delivery.id,
                "channel_id": delivery.channel_id,
                "main_id": delivery.main_id,
                "platform": channel.platform,
                "username": channel.username,
                "status": delivery.status,
                "attempts": delivery.attempts,
                "error_message": delivery.error_message,
                "sent_at": delivery.sent_at,
            }
            for delivery, channel in deliveries_result.all()
        ],
    }


async def apply_review(
    session,
    record: Main,
    *,
    action: str,
    title: str | None = None,
    lead: str | None = None,
    body: str | None = None,
) -> Main:
    """Apply approve/reject/save_draft with optional output edits.

    Transitions run through the ONE state machine
    (``app.services.transitions``): approve moves the record
    ``review -> approved`` (stamping ``approved_at``, first wins) and
    releases gated deliveries; reject moves to ``rejected``; save_draft
    edits only. The status column now tells the operator the truth — an
    approved record leaves the needs-review list immediately.

    Raises:
        ValueError: ``bad-action`` for unknown actions, ``not-in-review``
            for rows outside ``review`` (callers map to 422/409 or pages).
    """
    if action not in ("approve", "reject", "save_draft"):
        raise ValueError("bad-action")
    if record.status != "review":
        raise ValueError("not-in-review")
    if title is not None or lead is not None or body is not None:
        current = dict(record.output_json or {})
        if title is not None:
            current["title"] = title
        if lead is not None:
            current["lead"] = lead
        if body is not None:
            current["body"] = body
        record.output_json = current
    if action == "approve":
        # review -> approved (one place defines this: transitions module).
        assert_record_transition(record.status, "approved")
        record.status = "approved"
        if record.approved_at is None:
            record.approved_at = datetime.utcnow()
    elif action == "reject":
        assert_record_transition(record.status, "rejected")
        record.status = "rejected"
    record.updated_at = datetime.utcnow()
    await session.commit()
    if action == "approve":
        await confirm_deliveries_for_article(session, record.id)
    await session.refresh(record)
    return record


async def bulk_approve(session, record_ids: list[int]) -> tuple[list[int], list[int]]:
    """Approve many records; return ``(approved ids, skipped ids)``.

    Non-review and unknown rows are skipped, never forced — the backlog
    use-case must not overwrite decided or processing jobs.
    """
    approved, skipped = [], []
    for record_id in sorted(set(record_ids)):
        record = await get_record(session, record_id)
        if record is None or record.status != "review":
            skipped.append(record_id)
            continue
        await apply_review(session, record, action="approve")
        approved.append(record_id)
    return approved, skipped


async def record_status_counts(session) -> dict[str, int]:
    """Record counts per status (stats endpoint, dashboard, list KPIs)."""
    rows = (await session.execute(select(Main.status, func.count()).group_by(Main.status))).all()
    return {status: count for status, count in rows}


async def filter_options(session, limit: int = 50) -> dict[str, list[str]]:
    """Distinct field/university names for the list-page pickers.

    Panel list page (its own SQL, previously inline in the route) and any
    other caller of the shared filter vocabulary.
    """
    fields = [
        r[0] for r in (
            await session.execute(select(Main.field).distinct().order_by(Main.field).limit(limit))
        ).all() if r[0]
    ]
    universities = [
        r[0] for r in (
            await session.execute(
                select(Main.university).distinct().order_by(Main.university).limit(limit)
            )
        ).all() if r[0]
    ]
    return {"fields": fields, "universities": universities}


async def worker_heartbeat(session) -> object | None:
    """Latest ``main.heartbeat_at`` — the Worker freshness marker (dashboard).

    None when nothing was ever processed; the dashboard then reports the
    Worker as not-fresh rather than showing a stale timestamp.
    """
    return (
        await session.execute(select(func.max(Main.heartbeat_at)))
    ).scalar_one_or_none()


async def regenerate(session, record_id: int) -> Main:
    """Re-run LLM generation for a ``review``/``failed`` record (panel action).

    Uses the exact same resolution + generation path as the Worker
    (``get_active_*`` + shared ``resolve_generation_config`` +
    ``generate_news``), so a regenerated article is indistinguishable from
    one the Worker produced. Success stores the new output and returns the
    record to ``review`` (``failed -> review``, a declared transition);
    provider errors park it ``failed`` with the error text.

    Raises:
        ValueError: ``not-regenerable`` for a record outside
            ``review``/``failed`` (routes map to 409); any provider error
            propagates after the failure has been recorded on the record.
    """
    from app.news.generation import generate_news, resolve_generation_config
    from app.worker.worker import get_active_model, get_active_prompt

    record = await get_record(session, record_id)
    if record is None or record.status not in ("review", "failed"):
        raise ValueError("not-regenerable")

    active_model = await get_active_model(session)
    active_prompt = await get_active_prompt(session)
    # Same gap-fill as the Worker: keep per-record overrides, fill the rest.
    orig_model_id = record.model_id
    orig_prompt_id = record.prompt_id
    if record.model_id is None and active_model:
        record.model_id = active_model.id
    if record.prompt_id is None and active_prompt:
        record.prompt_id = active_prompt.id
    override_model = await session.get(Model, orig_model_id) if orig_model_id else None
    override_prompt = await session.get(Prompt, orig_prompt_id) if orig_prompt_id else None
    config = resolve_generation_config(override_model, override_prompt, active_model, active_prompt)

    thesis = {
        "title": record.title, "abstract": record.abstract, "field": record.field,
        "university": record.university, "table_of_contents": record.table_of_contents,
        "author": record.author, "supervisor": record.supervisor,
    }
    try:
        output = await generate_news(thesis, **config)
    except Exception as e:
        assert_record_transition(record.status, "failed")
        record.status = "failed"
        record.error_message = str(e)[:2000]
        record.updated_at = datetime.utcnow()
        await session.commit()
        raise
    record.output_json = {"title": output.title, "lead": output.lead, "body": output.body}
    assert_record_transition(record.status, "review")
    record.status = "review"
    record.error_message = None
    record.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(record)
    return record


async def needs_review_count(session) -> int:
    """Records awaiting a human decision: exactly the ``review`` rows.

    Approval moves the row to ``approved`` (and sending to ``published``),
    so this count is exact by construction — an approved record can never
    still appear as needing review.
    """
    return (
        await session.execute(
            select(func.count()).select_from(Main).where(Main.status == "review")
        )
    ).scalar_one()


async def approval_info(session, record: Main) -> tuple[str, object] | None:
    """``(username, timestamp)`` of the first approval audit, for the banner."""
    if record.approved_at is None:
        return None
    first = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.main_id == record.id, AuditLog.action == "approve")
            .order_by(AuditLog.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if first is None:
        return None
    approver = await session.get(PanelUser, first.user_id) if first.user_id else None
    return (approver.username if approver else "—", first.created_at)


async def records_per_day(session, days: int = 7) -> list[tuple[object, int]]:
    """``(date, count)`` of created records for the trailing ``days`` days."""
    from datetime import datetime, timedelta

    today = datetime.utcnow().date()
    out = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        start = datetime(day.year, day.month, day.day)
        out.append((
            day,
            (
                await session.execute(
                    select(func.count()).select_from(Main).where(
                        Main.created_at >= start,
                        Main.created_at < start + timedelta(days=1),
                    )
                )
            ).scalar_one(),
        ))
    return out


async def record_overview(session, record: Main) -> dict:
    """Everything the review page needs beyond the API payload, in one place.

    Deliveries with channel + category names, model/prompt names and texts,
    snapshot vs current category names, the field's matched keyword, routed
    channel display names, and audit history with usernames. Timestamps stay
    raw datetimes — routes format them for display.
    """
    from app.db.models import Category, Channel, Delivery, Field, Model, Prompt

    deliveries_rows = (
        await session.execute(
            select(Delivery).where(Delivery.main_id == record.id).order_by(Delivery.id)
        )
    ).scalars().all()
    deliveries = []
    for delivery in deliveries_rows:
        channel = await session.get(Channel, delivery.channel_id)
        category_name = None
        if delivery.category_id is not None:
            category = await session.get(Category, delivery.category_id)
            category_name = category.name if category else None
        deliveries.append({
            "id": delivery.id, "status": delivery.status, "attempts": delivery.attempts,
            "platform_message_id": delivery.platform_message_id,
            "error_message": delivery.error_message,
            "channel_name": (channel.username or channel.title or channel.platform) if channel else "—",
            "channel_username": channel.username if channel else None,
            "category_name": category_name, "route_source": delivery.route_source,
        })

    model_name = prompt_version = prompt_text = model_base_url = None
    model_row = await session.get(Model, record.model_id) if record.model_id else None
    if model_row is not None:
        model_name, model_base_url = model_row.name, model_row.base_url
    if model_name is None:
        active = (
            await session.execute(select(Model).where(Model.is_active.is_(True)).limit(1))
        ).scalar_one_or_none()
        if active is not None:
            model_name, model_base_url = active.name, active.base_url
    if record.prompt_id:
        prompt = await session.get(Prompt, record.prompt_id)
        if prompt is not None:
            prompt_version, prompt_text = prompt.version, prompt.system_prompt
    snapshot_category = current_category = field_keyword = None
    if record.category_id is not None:
        snapshot = await session.get(Category, record.category_id)
        snapshot_category = snapshot.name if snapshot else None
    if record.field_id is not None:
        field_row = await session.get(Field, record.field_id)
        if field_row is not None:
            field_keyword = field_row.matched_keyword
            if field_row.category_id is not None:
                current = await session.get(Category, field_row.category_id)
                current_category = current.name if current else None

    audits = (
        await session.execute(
            select(AuditLog).where(AuditLog.main_id == record.id).order_by(AuditLog.id.desc()).limit(20)
        )
    ).scalars().all()
    history = []
    for entry in audits:
        actor = await session.get(PanelUser, entry.user_id) if entry.user_id else None
        history.append({
            "title": entry.action, "username": actor.username if actor else None,
            "created_at": entry.created_at, "detail": entry.detail,
        })
    return {
        "deliveries": deliveries,
        "model_name": model_name, "model_base_url": model_base_url,
        "prompt_version": prompt_version, "prompt_text": prompt_text,
        "snapshot_category": snapshot_category, "current_category": current_category,
        "field_keyword": field_keyword, "history": history,
    }
