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
from app.schemas.news import (
    ClassificationResponse,
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


async def _record_detail_payload(session: AsyncSession, record) -> dict:
    """Build the extended record payload (shared by GET and PATCH)."""
    from app.services import records as record_service

    return await record_service.record_detail(session, record)


@router.get(
    "/records/{record_id}/classification",
    response_model=ClassificationResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Record not found"},
    },
)
async def get_record_classification(
    record_id: int = Path(..., description="The record identifier."),
    shared_Secret: str = Query("", description="Authentication secret."),
    session: AsyncSession = Depends(get_session),
):
    """Lightweight classification payload: only the stored matches.

    The v2-combined generation stores up to 3 ISCED-F 2013 matches per record
    (``main_category_matches``); this endpoint returns exactly those, with the
    primary flagged, without the full record body.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services import records as record_service

    record = await record_service.get_record(session, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return {
        "record_id": record.id,
        "category_id": record.category_id,
        "matches": await record_service.record_category_matches(session, record.id),
    }


@router.post(
    "/records/{record_id}/category-matches",
    response_model=ClassificationResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Record not found"},
        422: {"model": ErrorResponse, "description": "Unknown category name or invalid payload"},
    },
)
async def override_category_matches(
    record_id: int = Path(..., description="The record identifier."),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Manual override: a reviewer replaces the stored classification.

    Body: ``{ "shared_Secret", "matches": [{"category", "score", "reason"}] }``.
    The list REPLACES the previous matches (LLM answer included); the
    top-scoring entry becomes the primary category. Names must be exact live
    categories (the 29 ISCED-F 2013 names or ``other``); scores are clamped
    to [0, 1]. Audited.
    """
    from app.services import audit as audit_service
    from app.services import records as record_service
    from app.schemas.news import CategoryMatchesOverrideRequest

    _require_secret(payload)
    try:
        data = CategoryMatchesOverrideRequest(**(payload or {}))
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        await record_service.replace_category_matches(
            session,
            record_id,
            [
                {"category": m.category, "score": m.score, "reason": m.reason}
                for m in data.matches
            ],
        )
    except ValueError as e:
        message = str(e)
        if message.startswith("unknown-category:"):
            raise HTTPException(
                status_code=422, detail=f"Unknown category: {message.split(':', 1)[1]}"
            )
        if message == "not-found":
            raise HTTPException(status_code=404, detail="Record not found")
        if message == "too-many-matches":
            raise HTTPException(status_code=422, detail="At most 5 matches")
        raise HTTPException(status_code=422, detail=message)

    record = await record_service.get_record(session, record_id)
    matches = await record_service.record_category_matches(session, record_id)
    names = ", ".join(m["category"] for m in matches) or "empty"
    await audit_service.record_event(
        session,
        None,  # machine API: no panel user to attribute
        "override_category_matches",
        main_id=record_id,
        detail=f"matches: {names}"[:500],
    )
    logger.info("Manual category override record=%s matches=%s", record_id, names)
    return {
        "record_id": record_id,
        "category_id": record.category_id if record else None,
        "matches": matches,
    }


@router.post(
    "/records/{record_id}/republish",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Record not found"},
        409: {"model": ErrorResponse, "description": "Record not published/approved or no matches"},
    },
)
async def republish_record_endpoint(
    record_id: int = Path(...),
    payload: dict | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Re-queue deliveries for the (possibly corrected) categories of a
    published/approved record, without deleting anything already sent."""
    _require_secret(payload)
    body = payload or {}
    record = await record_service.get_record(session, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    try:
        queued = await record_service.republish_record(
            session, record, body.get("username", "api"),
        )
    except ValueError as e:
        message = str(e)
        if message == "not-published":
            raise HTTPException(status_code=409, detail="Record is not published or approved")
        if message == "no-matches":
            raise HTTPException(status_code=409, detail="Record has no category matches")
        raise HTTPException(status_code=422, detail=message)
    await audit_service.record_event(
        session, None, "republish", main_id=record_id,
        detail=f"queued={len(queued)}",
    )
    return {"record_id": record_id, "queued": queued}


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
    from app.services import deliveries as delivery_service

    body = payload or {}
    secret = body.get("shared_Secret")
    if not secret or not verify_secret(secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    found = await delivery_service.delivery_with_channel(session, delivery_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    delivery, channel = found
    try:
        delivery = await delivery_service.retry_delivery(session, delivery)
    except ValueError:
        raise HTTPException(status_code=409, detail="Delivery is not retryable")

    return delivery_service.delivery_info(delivery, channel)


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
    from app.services import deliveries as delivery_service

    body = payload or {}
    secret = body.get("shared_Secret")
    if not secret or not verify_secret(secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    found = await delivery_service.delivery_with_channel(session, delivery_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    delivery, channel = found
    try:
        delivery = await delivery_service.release_delivery(session, delivery)
    except ValueError:
        raise HTTPException(status_code=409, detail="Delivery is not awaiting confirmation")

    return delivery_service.delivery_info(delivery, channel)


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

    ``records_by_category`` counts records by their PRIMARY (rank-1) match,
    so a multi-category article is counted once, under its strongest match.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services import deliveries as delivery_service
    from app.services import records as record_service

    return {
        "records": await record_service.record_status_counts(session),
        "deliveries": await delivery_service.delivery_status_counts(session),
        "worker_last_heartbeat_at": await record_service.worker_heartbeat(session),
        "records_by_category": await record_service.records_by_category(session),
    }


@router.get(
    "/routing/preview",
    response_model=RoutingPreviewResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid shared_Secret"},
        404: {"model": ErrorResponse, "description": "Record not found"},
        422: {"model": ErrorResponse, "description": "Missing or invalid record_id"},
    },
)
async def routing_preview(
    shared_Secret: str = Query("", description="Authentication secret."),
    record_id: int = Query(..., description="Existing record to resolve."),
    session: AsyncSession = Depends(get_session),
):
    """Preview the routing chain for one record.

    Shows the record's display primary category and the channels the sender
    would use right now (with the category name that reached each). There is no
    field-name classification path; classification is stored per record.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.messaging.dispatcher import preview_routing

    try:
        return await preview_routing(session, record_id=record_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _model_response(row):
    """Build a model response that can never carry a key value.

    The ORM row has ``api_key_encrypted`` (ciphertext); the schema has no
    field for it and this projection fills only non-secret facts: whether a
    key exists, where it comes from, and the recorded probe outcome.
    """
    from app.schemas.news import ModelResponse
    from app.services import models as model_service

    creds = model_service.describe(row)
    return ModelResponse(
        id=row.id, name=row.name, provider=row.provider, version=row.version,
        is_active=row.is_active, base_url=creds["base_url"],
        api_key_env=creds["key_env"], api_model=creds["api_model"],
        has_key=bool(creds["key_present"]), key_source=creds["key_source"],
        needs_own_key=bool(creds["needs_own_key"]),
        temperature=row.temperature, max_output_tokens=row.max_output_tokens,
        description=row.description, created_at=row.created_at,
        last_tested_at=row.last_tested_at, last_test_ok=row.last_test_ok,
        last_test_error=row.last_test_error,
        last_test_latency_ms=row.last_test_latency_ms,
    )


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
    return [_model_response(r) for r in rows]


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
            api_model=data.api_model, api_key=data.api_key,
            temperature=data.temperature, max_output_tokens=data.max_output_tokens,
            description=data.description, is_active=data.is_active,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _model_response(row)


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
    return _model_response(row)


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
        for field in ("provider", "version", "base_url", "api_key_env", "api_model",
                      "temperature", "max_output_tokens", "description")
        if getattr(data, field) is not None
    }
    # api_key is write-only: accepted, encrypted, never echoed back.
    if data.api_key:
        updates["api_key"] = data.api_key
    row = await model_service.update_model(session, row, updates)
    return _model_response(row)


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
    return _model_response(row)


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
    import time

    started = time.perf_counter()
    ok, error = await model_service.test_model_connection(row)
    latency_ms = int((time.perf_counter() - started) * 1000)
    # Same recorded outcome the panel writes, so both surfaces agree.
    await model_service.record_test_result(session, row, ok=ok, error=error, latency_ms=latency_ms)
    return {
        "ok": ok,
        "model": row.name,
        "error": error,
        "latency_ms": latency_ms,
        "tested_at": row.last_tested_at,
    }


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
