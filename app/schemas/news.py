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
    model_id: int | None = Field(default=None, description="Optional per-record model override (must exist).")
    prompt_id: int | None = Field(default=None, description="Optional per-record prompt override (must exist).")

    @field_validator(
        "username", "title", "abstract", "field",
        "university", "table_of_contents", "author", "supervisor",
    )
    @classmethod
    def not_whitespace(cls, v: str) -> str:
        """Reject empty/whitespace-only strings (they pass min_length but carry nothing)."""
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


class CategoryMatchResponse(BaseModel):
    """One stored classification match, as reported to the API/panel."""

    category: str = Field(description="Exact ISCED-F 2013 category name (or \"other\").")
    category_id: int | None = None
    score: float = Field(description="Relevance 0.0–1.0 as stored (clamped).")
    rank: int = Field(description="1 = primary (highest score).")
    reason: str | None = Field(default=None, description="Brief Persian explanation.")
    is_primary: bool = Field(default=False, description="True when this is the record's display primary category.")


class ClassificationResponse(BaseModel):
    """Payload for ``GET /records/{id}/classification`` — the matches only."""

    record_id: int
    category_id: int | None = Field(default=None, description="Display primary category (top match).")
    matches: list[CategoryMatchResponse] = Field(default_factory=list)


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
    approved_at: datetime | None = None
    published_at: datetime | None = None
    field_id: int | None = None
    category_id: int | None = None
    model_id: int | None = None
    prompt_id: int | None = None
    deliveries: list[DeliveryInfo] = Field(default_factory=list)
    # The stored LLM classification (multi-category). Empty for records
    # generated before this feature or still awaiting generation.
    category_matches: list[CategoryMatchResponse] = Field(default_factory=list)


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


class StatsResponse(BaseModel):
    """``GET /stats`` payload: counts per status plus worker liveness."""

    records: dict[str, int]
    deliveries: dict[str, int]
    worker_last_heartbeat_at: datetime | None = None
    # Count of records whose PRIMARY (rank-1) classification is each category.
    records_by_category: dict[str, int] = Field(default_factory=dict)


class CategoryMatchInput(BaseModel):
    """One manual match supplied by a reviewer."""

    category: str = Field(min_length=1, description="Exact category name.")
    score: float = Field(ge=0.0, le=1.0)
    reason: str | None = Field(default=None, max_length=500)


class CategoryMatchesOverrideRequest(BaseModel):
    """Body for ``POST /records/{id}/category-matches`` — replace the matches.

    A reviewer's manual decision fully replaces the LLM's stored answer; the
    primary category becomes the first entry (highest score).
    """

    shared_Secret: str = Field(description="Authentication secret.")
    matches: list[CategoryMatchInput] = Field(default_factory=list, max_length=5)


class PreviewChannel(BaseModel):
    """One destination a field/record resolves to, with the route level."""

    id: int
    platform: str
    username: str | None = None
    title: str | None = None
    via: str = Field(description="Which route level produced it: category, field, or fallback.")


class RoutingPreviewResponse(BaseModel):
    """``GET /routing/preview`` payload: the auditable routing chain."""

    input: str = Field(description="The raw field name or record reference previewed.")
    record_id: int | None = None
    # None when nothing irreversible is known yet: classification now happens
    # inside the generation call, so a bare field name has no category until a
    # record carrying it has been generated (or a reviewer assigned one).
    category: str | None = None
    matched_keyword: str | None = Field(
        default=None,
        description="Legacy rule-based keyword. Always None for new records.",
    )
    channels: list[PreviewChannel]


class ServiceInfo(BaseModel):
    """Response for the root endpoint."""

    service: str = Field(json_schema_extra={"example": "IranDoc News Generation"})
    version: str = Field(json_schema_extra={"example": "1.0.0"})


class ErrorResponse(BaseModel):
    """Error payload returned by the API's HTTPException handlers."""

    detail: str = Field(json_schema_extra={"example": "Unauthorized"})


class CategoryMatch(BaseModel):
    """One ISCED category matched for an article (returned by the LLM).

    ``category`` must be one of the 29 EXACT ISCED-F 2013 Persian names (or
    "other"); anything else is treated as a hallucination and filtered by the
    worker. ``score`` is nominally 0.0–1.0 — values outside that range are
    clamped during post-processing rather than failing the whole generation.
    """

    category: str = Field(description="Exact ISCED-F 2013 category name (or \"other\").")
    score: float = Field(description="Relevance between 0.0 and 1.0.")
    reason: str = Field(description="Brief Persian explanation, one sentence max.")


class GeneratedNews(BaseModel):
    """Structured LLM output: news + classification in ONE response.

    The single call per thesis (v2-combined) writes the article and the
    ISCED-F 2013 classification together; ``categories`` defaults to [] so
    older constructors (tests, saved drafts) keep working unchanged.
    """

    title: str
    lead: str
    body: str
    categories: list[CategoryMatch] = Field(
        default_factory=list,
        max_length=3,
        description="Top ISCED-F 2013 categories, highest score first. Empty if none apply.",
    )


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
    approved_at: datetime | None = None

    model_config = {"from_attributes": True}


class RecordListResponse(BaseModel):
    """Paginated ``GET /records`` payload: items plus the total count."""

    items: list[RecordListItem]
    total: int
    page: int
    page_size: int


class ModelResponse(BaseModel):
    """One model row. NEVER includes a key value (PART D2).

    Reports where the credential comes from and whether one is usable, plus
    the recorded connection-probe outcome. No ciphertext, no plaintext, no
    masked hint of the secret itself.
    """

    id: int
    name: str
    provider: str
    version: str
    is_active: bool
    base_url: str | None = None
    api_key_env: str | None = None
    api_model: str | None = None
    has_key: bool = False
    key_source: str | None = None
    needs_own_key: bool = False
    temperature: float | None = None
    max_output_tokens: int | None = None
    description: str | None = None
    created_at: datetime | None = None
    last_tested_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_error: str | None = None
    last_test_latency_ms: int | None = None

    model_config = {"from_attributes": True}


class ModelCreateRequest(BaseModel):
    """Body for ``POST /models`` (creation only; activation via activate)."""

    shared_Secret: str = Field(description="Authentication secret.")
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    version: str = Field(min_length=1, max_length=50)
    base_url: str | None = Field(default=None, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=100)
    api_model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32000)
    description: str | None = None
    is_active: bool = False


class ModelUpdateRequest(BaseModel):
    """Body for ``PATCH /models/{id}`` (is_active only via activate).

    ``api_key`` is write-only: it is encrypted on the way in and never
    returned. Omitting it keeps the stored key; ``clear_api_key`` removes it.
    """

    shared_Secret: str = Field(description="Authentication secret.")
    provider: str | None = Field(default=None, min_length=1, max_length=50)
    version: str | None = Field(default=None, min_length=1, max_length=50)
    base_url: str | None = Field(default=None, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=100)
    api_model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32000)
    description: str | None = None


class PromptResponse(BaseModel):
    """One prompt version (immutable: new version = new row)."""

    id: int
    version: str
    system_prompt: str
    is_active: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class PromptCreateRequest(BaseModel):
    """Body for ``POST /prompts`` (new row; never edits in place)."""

    shared_Secret: str = Field(description="Authentication secret.")
    version: str = Field(min_length=1, max_length=50)
    system_prompt: str = Field(min_length=1)
    is_active: bool = False


class SecretBody(BaseModel):
    """Body carrying only the shared secret (activate/retry/test-connection)."""

    shared_Secret: str = Field(description="Authentication secret.")


class ConnectionTestResponse(BaseModel):
    """Result of ``POST /models/{id}/test-connection`` (key never included)."""

    ok: bool
    model: str
    error: str | None = None
