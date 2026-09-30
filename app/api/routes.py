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
    "/records/{record_id}",
    response_model=RecordResponse,
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
    valid secret and 404 for unknown record ids.
    """
    if not verify_secret(shared_Secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from sqlalchemy import select

    result = await session.execute(select(Main).where(Main.id == record_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return {
        "id": record.id,
        "status": record.status,
        "field": record.field,
        "output_json": record.output_json,
        "error_message": record.error_message,
    }
