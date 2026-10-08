"""Pydantic contracts for the HTTP API.

These models describe the actual request/response shapes of the existing
endpoints. They are used both for runtime validation (where the endpoint
applies them) and for OpenAPI/Swagger schema generation.
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ThesisInput(BaseModel):
    """Request body for ``POST /submit`` — the nine-field input contract.

    All nine fields are required. ``shared_Secret`` is the authentication
    secret and is checked by the endpoint BEFORE this model is applied, so a
    missing/invalid secret never reaches validation and never creates a
    database record. Extra fields are ignored.

    The examples below are fake placeholders for Swagger "Try it out" only.
    """

    model_config = {"json_schema_extra": {"example": {
        "shared_Secret": "REPLACE_WITH_REAL_SECRET",
        "username": "test_user",
        "title": "Sample Thesis Title",
        "abstract": "This is a sample abstract used for API testing.",
        "field": "پزشکی",
        "university": "Tehran University",
        "table_of_contents": "1. Introduction\n2. Methods\n3. Results",
        "author": "John Doe",
        "supervisor": "Dr. Smith",
    }}}

    shared_Secret: str = Field(
        ...,
        json_schema_extra={"example": "REPLACE_WITH_REAL_SECRET"},
        description="Authentication secret. Checked before payload validation.",
    )
    username: str = Field(min_length=1, max_length=255, json_schema_extra={"example": "test_user"})
    title: str = Field(min_length=1, max_length=500, json_schema_extra={"example": "Sample Thesis Title"})
    abstract: str = Field(min_length=1, max_length=2000, json_schema_extra={"example": "This is a sample abstract used for API testing."})
    field: str = Field(min_length=1, max_length=200, json_schema_extra={"example": "پزشکی"})
    university: str = Field(min_length=1, max_length=255, json_schema_extra={"example": "Tehran University"})
    table_of_contents: str = Field(min_length=1, max_length=5000, json_schema_extra={"example": "1. Introduction\n2. Methods\n3. Results"})
    author: str = Field(min_length=1, max_length=255, json_schema_extra={"example": "John Doe"})
    supervisor: str = Field(min_length=1, max_length=255, json_schema_extra={"example": "Dr. Smith"})

    @field_validator(
        "username", "title", "abstract", "field",
        "university", "table_of_contents", "author", "supervisor",
    )
    @classmethod
    def not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be whitespace-only")
        return v


class SubmitResponse(BaseModel):
    """Response for a valid submission: the record id and initial status."""

    id: int = Field(json_schema_extra={"example": 1})
    status: str = Field(json_schema_extra={"example": "ready"})


class HealthResponse(BaseModel):
    """Health endpoint payload: application and database connectivity."""

    status: str = Field(json_schema_extra={"example": "ok"})
    database: str = Field(json_schema_extra={"example": "connected"})


class RecordResponse(BaseModel):
    """Response for ``GET /records/{record_id}`` — the stored record state."""

    id: int = Field(json_schema_extra={"example": 1})
    status: str = Field(json_schema_extra={"example": "review"})
    field: str = Field(json_schema_extra={"example": "پزشکی"})
    output_json: dict | None = Field(default=None, json_schema_extra={"example": {"title": "...", "lead": "...", "body": "..."}})
    error_message: str | None = Field(default=None)


class DeliveryInfo(BaseModel):
    """One delivery row with its channel destination for record detail."""

    id: int
    channel_id: int
    main_id: int | None = None
    platform: str | None = None
    username: str | None = None
    status: str
    attempts: int
    error_message: str | None = None
    sent_at: datetime | None = None

    model_config = {"from_attributes": True}


class RecordDetailResponse(RecordResponse):
    """Extended ``GET /records/{id}`` payload for the review panel.

    Keeps every ``RecordResponse`` field unchanged and adds thesis metadata,
    audit FKs, timestamps, and per-channel delivery state (the channel routes).
    """

    author: str | None = None
    supervisor: str | None = None
    university: str | None = None
    table_of_contents: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    field_id: int | None = None
    category_id: int | None = None
    model_id: int | None = None
    prompt_id: int | None = None
    deliveries: list[DeliveryInfo] = Field(default_factory=list)


class ReviewActionRequest(BaseModel):
    """Request body for ``PATCH /records/{id}`` — a human review decision.

    ``shared_Secret`` keeps machine-call auth consistent with ``POST /submit``.
    ``action`` is the decision; optional ``title``/``lead``/``body`` edit the
    stored ``output_json`` before the transition (used by save_draft and to fix
    typos during approve/reject).
    """

    shared_Secret: str = Field(description="Authentication secret.")
    action: str = Field(description="One of approve, reject, save_draft.")
    title: str | None = Field(default=None, min_length=1)
    lead: str | None = Field(default=None, min_length=1)
    body: str | None = Field(default=None, min_length=1)


class DeliveryListResponse(BaseModel):
    """Paginated ``GET /deliveries`` payload for the review panel."""

    items: list[DeliveryInfo]
    total: int
    page: int
    page_size: int


class DeliveryRetryRequest(BaseModel):
    """Request body for ``POST /deliveries/{id}/retry``."""

    shared_Secret: str = Field(description="Authentication secret.")


class ServiceInfo(BaseModel):
    """Response for the root endpoint."""

    service: str = Field(json_schema_extra={"example": "IranDoc News Generation"})
    version: str = Field(json_schema_extra={"example": "1.0.0"})


class ErrorResponse(BaseModel):
    """Error payload returned by the API's HTTPException handlers."""

    detail: str = Field(json_schema_extra={"example": "Unauthorized"})


class GeneratedNews(BaseModel):
    """Structured LLM output contract (PydanticAI ``output_type``)."""

    title: str
    lead: str
    body: str


class RecordListItem(BaseModel):
    """One row of the paginated record listing for the review panel."""

    id: int
    status: str
    field: str
    university: str
    title: str
    author: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RecordListResponse(BaseModel):
    """Paginated ``GET /records`` payload: items plus the total count."""

    items: list[RecordListItem]
    total: int
    page: int
    page_size: int
