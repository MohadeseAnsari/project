"""HTTP API routes.

The request path only authenticates, validates, and stores a ``ready`` job —
it never performs LLM generation (Assignment 3: "The HTTP request path must
not perform LLM generation"). Generation happens asynchronously in the
Worker.

The OpenAPI declarations on each endpoint (``response_model``, ``Path``,
``Query``, ``responses``) describe the existing behavior so Swagger UI can be
used as an interactive testing tool. They do not change the endpoint logic.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.limits import limiter
from app.auth.secret import verify_secret
from app.db.connection import get_session
from app.db.models import Main
from app.schemas.news import (
    ErrorResponse,
    HealthResponse,
    RecordDetailResponse,
    RecordListResponse,
    RecordResponse,
    ServiceInfo,
    SubmitResponse,
    ThesisInput,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/submit",
    response_model=SubmitResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid JSON body"},
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        422: {"model": ErrorResponse, "description": "Payload failed validation"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
@limiter.limit("10/minute")
async def submit(request: Request, session: AsyncSession = Depends(get_session)):
    """Submit a thesis and store it as a ``ready`` job.

    Order of checks (required by the assignment): parse JSON → authenticate
    ``shared_Secret`` → validate the payload. Invalid or missing secrets are
    rejected with 401 BEFORE validation, so a failed authentication never
    creates a database record.

    Returns:
        ``{"id": <int>, "status": "ready"}`` with HTTP 200.

    Raises:
        400: malformed JSON body.
        401: missing or invalid ``shared_Secret``.
        422: payload fails Pydantic validation.
        500: unexpected database error (no record is created).
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    secret = body.get("shared_Secret")
    if not secret or not verify_secret(secret):
        logger.warning("Authentication failed: invalid or missing shared_Secret")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = ThesisInput(**body)
    except Exception as e:
        logger.warning("Validation failed: %s", e)
        raise HTTPException(status_code=422, detail=str(e))

    try:
        record = Main(
            username=data.username,
            title=data.title,
            abstract=data.abstract,
            field=data.field,
            university=data.university,
            table_of_contents=data.table_of_contents,
            author=data.author,
            supervisor=data.supervisor,
            status="ready",
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
    except Exception as e:
        logger.error("Database error during submission: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

    logger.info("Accepted submission id=%s field=%s", record.id, record.field)
    return SubmitResponse(id=record.id, status=record.status)


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={500: {"model": ErrorResponse, "description": "Unexpected error"}},
)
async def health(session: AsyncSession = Depends(get_session)):
    """Report application health.

    Distinguishes "container/process alive" from "application healthy": the
    endpoint only reports ``ok``/``connected`` when a real ``SELECT 1``
    against PostgreSQL succeeds. Worker liveness is intentionally not
    included — the Worker is a batch process with no HTTP surface, and the
    assignments do not require worker state in the health endpoint.
    """
    try:
        await session.execute(text("SELECT 1"))
        return HealthResponse(status="ok", database="connected")
    except Exception:
        return HealthResponse(status="unhealthy", database="disconnected")


@router.get(
    "/records",
    response_model=RecordListResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        422: {"model": ErrorResponse, "description": "Invalid pagination or order"},
    },
)
async def list_records(
    shared_Secret: str = Query(
        "",
        example="REPLACE_WITH_REAL_SECRET",
        description="Authentication secret. Required to read records.",
    ),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(20, ge=1, le=100, description="Rows per page (max 100)."),
    status: str | None = Query(None, description="Exact status filter."),
    field: str | None = Query(None, description="Substring filter on field (case-insensitive)."),
    university: str | None = Query(None, description="Substring filter on university (case-insensitive)."),
    q: str | None = Query(None, description="Search across title/abstract/field/university/author/supervisor."),
    order: str = Query("newest", description="Result order: newest (default) or oldest."),
    session: AsyncSession = Depends(get_session),
):
    """List records with SQL LIMIT/OFFSET pagination for the review panel.

    Filtering happens inside the query (not in Python) so ``total`` reflects
    the filtered set and pages beyond the data are empty rather than stale.
    ``q`` is a case-insensitive substring match across the main thesis text
    columns; ``field``/``university`` narrow it further. ``order`` selects
    creation order only, keeping the listing deterministic for review.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if order not in ("newest", "oldest"):
        raise HTTPException(status_code=422, detail="order must be newest or oldest")

    from sqlalchemy import func, or_, select

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
                Main.title.ilike(like),
                Main.abstract.ilike(like),
                Main.field.ilike(like),
                Main.university.ilike(like),
                Main.author.ilike(like),
                Main.supervisor.ilike(like),
            )
        )

    count_result = await session.execute(
        select(func.count()).select_from(Main).where(*filters) if filters else select(func.count()).select_from(Main)
    )
    total = count_result.scalar_one()

    ordering = (
        [Main.created_at.desc(), Main.id.desc()]
        if order == "newest"
        else [Main.created_at.asc(), Main.id.asc()]
    )
    query = select(Main)
    if filters:
        query = query.where(*filters)
    query = query.order_by(*ordering).limit(page_size).offset((page - 1) * page_size)
    result = await session.execute(query)
    rows = list(result.scalars().all())
    return {
        "items": [
            {
                "id": r.id,
                "status": r.status,
                "field": r.field,
                "university": r.university,
                "title": r.title,
                "author": r.author,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get(
    "/records/{record_id}",
    response_model=RecordDetailResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Record not found"},
    },
)
async def get_record(
    record_id: int = Path(
        ...,
        example=1,
        description="The record identifier returned by POST /submit.",
    ),
    shared_Secret: str = Query(
        "",
        example="REPLACE_WITH_REAL_SECRET",
        description="Authentication secret. Required to read the record.",
    ),
    session: AsyncSession = Depends(get_session),
):
    """Retrieve a record's status and (when finished) its generated output.

    Requires the shared secret as a query parameter. Returns 401 without a
    valid secret and 404 for unknown record ids. The extended payload keeps the
    original fields and adds thesis metadata, audit FKs, timestamps, and the
    per-channel delivery state for the review panel.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from sqlalchemy import select

    from app.db.models import Channel, Delivery

    result = await session.execute(select(Main).where(Main.id == record_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    deliveries_result = await session.execute(
        select(Delivery, Channel)
        .join(Channel, Channel.id == Delivery.channel_id)
        .where(Delivery.main_id == record.id)
        .order_by(Delivery.id)
    )
    deliveries = [
        {
            "id": delivery.id,
            "channel_id": delivery.channel_id,
            "platform": channel.platform,
            "username": channel.username,
            "status": delivery.status,
            "attempts": delivery.attempts,
            "error_message": delivery.error_message,
            "sent_at": delivery.sent_at,
        }
        for delivery, channel in deliveries_result.all()
    ]
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
        "field_id": record.field_id,
        "category_id": record.category_id,
        "model_id": record.model_id,
        "prompt_id": record.prompt_id,
        "deliveries": deliveries,
    }
