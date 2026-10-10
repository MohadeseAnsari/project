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
    ConnectionTestResponse,
    DeliveryInfo,
    DeliveryListResponse,
    ErrorResponse,
    HealthResponse,
    ModelResponse,
    PromptResponse,
    RecordDetailResponse,
    RecordListResponse,
    RoutingPreviewResponse,
    StatsResponse,
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
        from app.services import records as record_service

        try:
            record = await record_service.create_thesis_record(
                session,
                username=data.username,
                title=data.title,
                abstract=data.abstract,
                field=data.field,
                university=data.university,
                table_of_contents=data.table_of_contents,
                author=data.author,
                supervisor=data.supervisor,
                model_id=data.model_id,
                prompt_id=data.prompt_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
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
    approved: bool | None = Query(None, description="True: only approved; False: only needs-review; omit: all."),
    session: AsyncSession = Depends(get_session),
):
    """List records with SQL LIMIT/OFFSET pagination for the review panel.

    Filtering happens inside the query (not in Python) so ``total`` reflects
    the filtered set and pages beyond the data are empty rather than stale.
    ``q`` is a case-insensitive substring match across the main thesis text
    columns; ``field``/``university`` narrow it further. ``order`` selects
    creation order only, keeping the listing deterministic for review.
    ``approved`` splits reviewed rows into approved vs still-needs-review
    (``approved_at`` set or not); omitted means both.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if order not in ("newest", "oldest"):
        raise HTTPException(status_code=422, detail="order must be newest or oldest")

    from app.services import records as record_service

    rows, total = await record_service.list_records(
        session, page=page, page_size=page_size, status=status, field=field,
        university=university, q=q, order=order, approved=approved,
    )
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
                "approved_at": r.approved_at,
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

    from app.services import records as record_service

    record = await record_service.get_record(session, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return await _record_detail_payload(session, record)


async def _record_detail_payload(session: AsyncSession, record: Main) -> dict:
    """Build the extended record payload (shared by GET and PATCH)."""
    from app.services import records as record_service

    return await record_service.record_detail(session, record)


@router.patch(
    "/records/{record_id}",
    response_model=RecordDetailResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Record not found"},
        409: {"model": ErrorResponse, "description": "Record is not in review state"},
        422: {"model": ErrorResponse, "description": "Invalid action or edit"},
    },
)
async def review_record(
    record_id: int = Path(..., description="The record identifier."),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Apply a human review decision to a ``review`` record.

    Only ``review`` rows may transition (409 otherwise) so an already-decided
    or still-processing job is never overwritten. ``approve`` stamps
    ``approved_at`` (first approval wins) and flips gated
    ``awaiting_confirmation`` deliveries to ``pending``; ``reject`` moves the
    record to ``rejected`` (gated deliveries stay gated and are therefore never
    sent); ``save_draft`` only edits the stored ``output_json``. Optional
    ``title``/``lead``/``body`` edit the output before the transition.
    """
    from datetime import datetime

    from sqlalchemy import select

    from app.schemas.news import ReviewActionRequest
    from app.services import records as record_service

    body = payload or {}
    secret = body.get("shared_Secret")
    if not secret or not verify_secret(secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        data = ReviewActionRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    if data.action not in ("approve", "reject", "save_draft"):
        raise HTTPException(status_code=422, detail="action must be approve, reject or save_draft")

    record = await record_service.get_record(session, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    try:
        record = await record_service.apply_review(
            session, record, action=data.action,
            title=data.title, lead=data.lead, body=data.body,
        )
    except ValueError as e:
        if str(e) == "not-in-review":
            raise HTTPException(status_code=409, detail="Record is not in review state")
        raise HTTPException(status_code=422, detail=str(e))

    return await _record_detail_payload(session, record)


@router.get(
    "/deliveries",
    response_model=DeliveryListResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        422: {"model": ErrorResponse, "description": "Invalid pagination"},
    },
)
async def list_deliveries(
    shared_Secret: str = Query("", description="Authentication secret."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(20, ge=1, le=100, description="Rows per page (max 100)."),
    status: str | None = Query(None, description="Exact delivery status filter."),
    platform: str | None = Query(None, description="Channel platform filter (bale|telegram)."),
    main_id: int | None = Query(None, description="Article id filter."),
    session: AsyncSession = Depends(get_session),
):
    """List deliveries with SQL LIMIT/OFFSET pagination for the review panel.

    Filtering happens inside the query so ``total`` reflects the filtered set.
    ``platform`` filters on the joined channel row because a delivery alone does
    not carry its platform.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services import deliveries as delivery_service

    pairs, total = await delivery_service.fetch_deliveries(
        session, page=page, page_size=page_size, status=status,
        platform=platform, main_id=main_id,
    )
    return {
        "items": [
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
            for delivery, channel, _article in pairs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post(
    "/deliveries/{delivery_id}/retry",
    response_model=DeliveryInfo,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Delivery not found"},
        409: {"model": ErrorResponse, "description": "Delivery is not retryable"},
    },
)
async def retry_delivery(
    delivery_id: int = Path(..., description="The delivery identifier."),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Re-queue a ``failed`` delivery as ``pending`` for the review panel.

    Only ``failed`` rows are retryable (409 otherwise) so a send in flight or
    an already-sent article is never duplicated by a retry click. Attempts are
    reset to 0 (owner-approved) so the sender's bounded retry budget restarts.
    """
    from app.db.models import Channel
    from app.services import deliveries as delivery_service

    body = payload or {}
    secret = body.get("shared_Secret")
    if not secret or not verify_secret(secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    delivery = await delivery_service.get_delivery(session, delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    try:
        delivery = await delivery_service.retry_delivery(session, delivery)
    except ValueError:
        raise HTTPException(status_code=409, detail="Delivery is not retryable")

    channel = await session.get(Channel, delivery.channel_id)
    return {
        "id": delivery.id,
        "channel_id": delivery.channel_id,
        "main_id": delivery.main_id,
        "platform": channel.platform if channel else None,
        "username": channel.username if channel else None,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "error_message": delivery.error_message,
        "sent_at": delivery.sent_at,
    }


@router.post(
    "/deliveries/{delivery_id}/release",
    response_model=DeliveryInfo,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Delivery not found"},
        409: {"model": ErrorResponse, "description": "Delivery is not awaiting confirmation"},
    },
)
async def release_delivery(
    delivery_id: int = Path(..., description="The delivery identifier."),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Release one gated delivery for sending (the per-delivery approve).

    Only ``awaiting_confirmation`` rows move (409 otherwise): already-queued,
    in-flight, sent, or failed rows are never touched, so a release click
    cannot duplicate a send. Unlike retry, attempts are kept — this is the
    delivery's first release, not a re-queue after failure.
    """
    from app.db.models import Channel
    from app.services import deliveries as delivery_service

    body = payload or {}
    secret = body.get("shared_Secret")
    if not secret or not verify_secret(secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    delivery = await delivery_service.get_delivery(session, delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    try:
        delivery = await delivery_service.release_delivery(session, delivery)
    except ValueError:
        raise HTTPException(status_code=409, detail="Delivery is not awaiting confirmation")

    channel = await session.get(Channel, delivery.channel_id)
    return {
        "id": delivery.id,
        "channel_id": delivery.channel_id,
        "main_id": delivery.main_id,
        "platform": channel.platform if channel else None,
        "username": channel.username if channel else None,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "error_message": delivery.error_message,
        "sent_at": delivery.sent_at,
    }


@router.get(
    "/stats",
    response_model=StatsResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
    },
)
async def get_stats(
    shared_Secret: str = Query("", description="Authentication secret."),
    session: AsyncSession = Depends(get_session),
):
    """Return record/delivery counts per status plus worker heartbeat freshness.

    Counts come from GROUP BY queries (no row fetching) so the panel can show
    queue depth at a glance. Freshness is the newest ``main.heartbeat_at`` —
    the last time the generation Worker touched a job — and nothing else: no
    per-record processing times are exposed here (the demo must not show them).
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from sqlalchemy import func, select

    from app.services import deliveries as delivery_service
    from app.services import records as record_service

    last_heartbeat = (
        await session.execute(select(func.max(Main.heartbeat_at)))
    ).scalar_one_or_none()
    return {
        "records": await record_service.record_status_counts(session),
        "deliveries": await delivery_service.delivery_status_counts(session),
        "worker_last_heartbeat_at": last_heartbeat,
    }


@router.get(
    "/routing/preview",
    response_model=RoutingPreviewResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Record not found"},
        422: {"model": ErrorResponse, "description": "Pass exactly one of field or record_id"},
    },
)
async def routing_preview(
    shared_Secret: str = Query("", description="Authentication secret."),
    field: str | None = Query(None, description="Raw field name to resolve."),
    record_id: int | None = Query(None, description="Existing record to resolve."),
    session: AsyncSession = Depends(get_session),
):
    """Preview the routing chain: category, matched keyword, destination channels.

    Read-only audit surface for the client: given a raw field name (or an
    existing record), show which category it resolves to, which keyword
    decided it, and which channels the sender would use right now (with the
    route level of each). Uses the same resolver as the sender and the panel.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.messaging.dispatcher import preview_routing

    try:
        return await preview_routing(session, field=field, record_id=record_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _require_secret(body: dict | None) -> None:
    """Reject missing/invalid shared secrets for body-authenticated calls."""
    secret = (body or {}).get("shared_Secret")
    if not secret or not verify_secret(secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/models", response_model=list[ModelResponse])
async def list_models(
    shared_Secret: str = Query("", description="Authentication secret."),
    session: AsyncSession = Depends(get_session),
):
    """List model configurations (key values never included)."""
    from app.schemas.news import ModelResponse
    from app.services import models as model_service

    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = await model_service.list_models(session)
    return [ModelResponse.model_validate(r) for r in rows]


@router.post("/models", response_model=ModelResponse, status_code=201)
async def create_model(
    payload: dict | None = None, session: AsyncSession = Depends(get_session)
):
    """Create a model row (inactive by default; use activate for single-active)."""
    from app.schemas.news import ModelCreateRequest, ModelResponse
    from app.services import models as model_service

    _require_secret(payload)
    try:
        data = ModelCreateRequest(**(payload or {}))
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        row = await model_service.create_model(
            session, name=data.name, provider=data.provider, version=data.version,
            base_url=data.base_url, api_key_env=data.api_key_env,
            temperature=data.temperature, max_output_tokens=data.max_output_tokens,
            description=data.description, is_active=data.is_active,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return ModelResponse.model_validate(row)


@router.get("/models/{model_id}", response_model=ModelResponse)
async def get_model(
    model_id: int = Path(...),
    shared_Secret: str = Query("", description="Authentication secret."),
    session: AsyncSession = Depends(get_session),
):
    """Retrieve one model configuration."""
    from app.schemas.news import ModelResponse
    from app.services import models as model_service

    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    row = await model_service.get_model(session, model_id)
    if not row:
        raise HTTPException(status_code=404, detail="Model not found")
    return ModelResponse.model_validate(row)


@router.patch("/models/{model_id}", response_model=ModelResponse)
async def update_model(
    model_id: int = Path(...),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Update a model row (is_active only via activate, never here)."""
    from app.schemas.news import ModelResponse, ModelUpdateRequest
    from app.services import models as model_service

    _require_secret(payload)
    body = dict(payload or {})
    if "is_active" in body:
        raise HTTPException(status_code=422, detail="use POST /models/{id}/activate")
    try:
        data = ModelUpdateRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    row = await model_service.get_model(session, model_id)
    if not row:
        raise HTTPException(status_code=404, detail="Model not found")
    updates = {
        field: getattr(data, field)
        for field in ("provider", "version", "base_url", "api_key_env", "temperature", "max_output_tokens", "description")
        if getattr(data, field) is not None
    }
    row = await model_service.update_model(session, row, updates)
    return ModelResponse.model_validate(row)


@router.post("/models/{model_id}/activate", response_model=ModelResponse)
async def activate_model(
    model_id: int = Path(...),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Activate one model atomically (exactly one active row afterwards)."""
    from app.schemas.news import ModelResponse
    from app.services import models as model_service

    _require_secret(payload)
    row = await model_service.get_model(session, model_id)
    if not row:
        raise HTTPException(status_code=404, detail="Model not found")
    row = await model_service.activate_model(session, row)
    return ModelResponse.model_validate(row)


@router.post("/models/{model_id}/test-connection", response_model=ConnectionTestResponse)
async def test_model_connection(
    model_id: int = Path(...),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Make a real minimal provider call for one model (key never returned/logged).

    Uses the shared probe helper, so the machine API and the panel test button
    report the same result for the same row. Missing keys, DNS failures, auth
    errors, and timeouts all return ``ok:false`` with the error text
    (truncated) instead of raising, so the panel can show the failure without
    leaking credentials.
    """
    from app.services import models as model_service

    _require_secret(payload)
    row = await model_service.get_model(session, model_id)
    if not row:
        raise HTTPException(status_code=404, detail="Model not found")
    ok, error = await model_service.test_model_connection(row)
    return {"ok": ok, "model": row.name, "error": error}


@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts(
    shared_Secret: str = Query("", description="Authentication secret."),
    session: AsyncSession = Depends(get_session),
):
    """List prompt versions (newest last; immutable rows)."""
    from app.schemas.news import PromptResponse
    from app.services import prompts as prompt_service

    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = await prompt_service.list_prompts(session)
    return [PromptResponse.model_validate(r) for r in rows]


@router.post("/prompts", response_model=PromptResponse, status_code=201)
async def create_prompt(
    payload: dict | None = None, session: AsyncSession = Depends(get_session)
):
    """Create a new prompt version (never edits in place; activate to release)."""
    from app.schemas.news import PromptCreateRequest, PromptResponse
    from app.services import prompts as prompt_service

    _require_secret(payload)
    try:
        data = PromptCreateRequest(**(payload or {}))
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        row = await prompt_service.create_prompt(
            session, version=data.version, system_prompt=data.system_prompt,
            is_active=data.is_active,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return PromptResponse.model_validate(row)


@router.get("/prompts/{prompt_id}", response_model=PromptResponse)
async def get_prompt(
    prompt_id: int = Path(...),
    shared_Secret: str = Query("", description="Authentication secret."),
    session: AsyncSession = Depends(get_session),
):
    """Retrieve one prompt version."""
    from app.schemas.news import PromptResponse
    from app.services import prompts as prompt_service

    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    row = await prompt_service.get_prompt(session, prompt_id)
    if not row:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return PromptResponse.model_validate(row)


@router.post("/prompts/{prompt_id}/activate", response_model=PromptResponse)
async def activate_prompt(
    prompt_id: int = Path(...),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Activate one prompt version atomically (exactly one active row)."""
    from app.schemas.news import PromptResponse
    from app.services import prompts as prompt_service

    _require_secret(payload)
    row = await prompt_service.get_prompt(session, prompt_id)
    if not row:
        raise HTTPException(status_code=404, detail="Prompt not found")
    row = await prompt_service.activate_prompt(session, row)
    return PromptResponse.model_validate(row)
