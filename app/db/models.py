"""SQLAlchemy ORM models mirroring the migrated PostgreSQL schema.

The schema itself is owned by ``migrations/*.sql`` (applied once-only by
``scripts/apply_migrations.py``); these classes exist so application code can
query the same tables with the async session. Every table here has a matching
migration — never add a model without one.
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models (schema is managed by migrations)."""


class Category(Base):
    """A destination bucket for classification.

    Two vocabularies met here during the v2-combined migration. The LLM
    classifies against the 29 ISCED-F 2013 intermediate fields (+ the ``other``
    fallback); those rows have ``deprecated = FALSE`` and are the only ones a
    NEW record references. The old rule-based categories were remapped onto
    these rows and deleted by migration 024, so historical
    ``fields``/``main``/``deliveries`` FKs point at live ISCED rows (or NULL
    where no honest equivalent existed). The ``deprecated`` flag remains for
    future retirement without breaking FKs.

    ``isc_code`` records the indicative ISCED-F 2013 reference code (metadata
    only; routing never reads it).
    """

    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    deprecated = Column(Boolean, nullable=False, default=False, server_default="false")
    isc_code = Column(String(10), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    fields = relationship("Field", back_populates="category")
    main_records = relationship("Main", back_populates="category")
    channel_routes = relationship(
        "CategoryChannelRoute", back_populates="category", cascade="all, delete-orphan"
    )
    main_matches = relationship(
        "MainCategoryMatch", back_populates="category", cascade="all, delete-orphan"
    )


class Field(Base):
    """A known thesis field.

    ``normalized_name`` is the stable identity used for exact matching and
    duplicate prevention; ``source`` distinguishes spreadsheet-seeded fields
    from runtime-discovered ones. ``category_id`` is DEPRECATED for new rows —
    it is kept (nullable now) for the historical, rule-based classification of
    previously seen fields and for the panel's manual curation of that legacy
    metadata; classification of a RECORD now lives in
    ``main_category_matches``. ``matched_keyword`` is likewise legacy: new
    runtime fields insert it as NULL.
    """

    __tablename__ = "fields"

    id = Column(Integer, primary_key=True)
    name = Column(String(500), unique=True, nullable=False)
    normalized_name = Column(String(500), unique=True, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    source = Column(String(20), nullable=False, default="spreadsheet")
    matched_keyword = Column(String(200), nullable=True)
    category_source = Column(String(20), nullable=False, default="rules")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    category = relationship("Category", back_populates="fields")
    main_records = relationship("Main", back_populates="field_record")
    channel_routes = relationship(
        "FieldChannelRoute", back_populates="field_record", cascade="all, delete-orphan"
    )


class Model(Base):
    """LLM model configuration; ``is_active`` marks the model the Worker uses.

    ``base_url`` overrides the provider endpoint per model; ``api_key_env`` is
    the ENVIRONMENT VARIABLE NAME holding the key (never the key itself).
    """

    __tablename__ = "models"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    provider = Column(String(50), nullable=False)
    version = Column(String(50), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    base_url = Column(String(500), nullable=True)
    api_key_env = Column(String(100), nullable=True)
    temperature = Column(Float, nullable=True)
    max_output_tokens = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    #: Fernet ciphertext of the provider key (never plaintext). Managed from
    #: the panel; the master key lives in CREDENTIALS_KEY, never in the DB.
    api_key_encrypted = Column(Text, nullable=True)
    #: The provider's real model id, when it differs from the display ``name``.
    api_model = Column(String(200), nullable=True)
    #: Connection-probe outcome (endpoint + HTTP status only, never the key).
    last_tested_at = Column(DateTime, nullable=True)
    last_test_ok = Column(Boolean, nullable=True)
    last_test_error = Column(Text, nullable=True)
    last_test_latency_ms = Column(Integer, nullable=True)

    main_records = relationship("Main", back_populates="model")


class Prompt(Base):
    """A versioned system prompt; ``is_active`` marks the prompt the Worker uses."""

    __tablename__ = "prompts"

    id = Column(Integer, primary_key=True)
    version = Column(String(50), unique=True, nullable=False)
    system_prompt = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    main_records = relationship("Main", back_populates="prompt")


class Role(Base):
    """A panel role (admin full access, editor review only)."""

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(20), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    users = relationship("PanelUser", back_populates="role")


class PanelUser(Base):
    """A human panel account (argon2 password hash, never plaintext)."""

    __tablename__ = "panel_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    #: Set when an admin deletes the account. The row is kept so
    #: ``audit_logs.user_id`` keeps resolving an author, but it is filtered out
    #: of every panel listing and can no longer log in.
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    role = relationship("Role", back_populates="users")


class AuditLog(Base):
    """Who did what to which record (review/retry/config changes)."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("panel_users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), nullable=False)
    main_id = Column(Integer, ForeignKey("main.id", ondelete="SET NULL"), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Main(Base):
    """The thesis/news record and job-state queue entry.

    Status lifecycle (one state machine, ``app/services/transitions.py``):
    ``ready`` → ``processing`` → ``review`` → ``approved`` → ``published``,
    plus ``rejected`` (human rejection from ``review``) and ``failed``.
    ``claimed_at``/``heartbeat_at`` support Worker crash recovery;
    ``field_id``/``model_id``/``prompt_id`` provide auditability of the field
    identity and generation configuration used; ``approved_at``/``published_at``
    record the decision/publication times.

    ``category_id`` is the DISPLAY primary category only (the LLM's top
    match, or a reviewer's manual choice). It is deprecated for routing:
    delivery resolution reads ``main_category_matches``.
    """

    __tablename__ = "main"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready', 'processing', 'review', 'approved', 'published', 'failed', 'rejected')",
            name="ck_main_status",
        ),
        Index("ix_main_status_created_at", "status", "created_at"),
        Index("ix_main_field_id", "field_id"),
        Index("ix_main_category_id", "category_id"),
    )

    id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=False)
    title = Column(Text, nullable=False)
    abstract = Column(Text, nullable=False)
    field = Column(String(500), nullable=False)
    university = Column(String(255), nullable=False)
    table_of_contents = Column(Text, nullable=True)
    author = Column(String(255), nullable=False)
    supervisor = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default="ready")
    field_id = Column(Integer, ForeignKey("fields.id"), nullable=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=True)
    prompt_id = Column(Integer, ForeignKey("prompts.id"), nullable=True)
    output_json = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    error_message = Column(Text, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    field_record = relationship("Field", back_populates="main_records")
    category = relationship("Category", back_populates="main_records")
    model = relationship("Model", back_populates="main_records")
    prompt = relationship("Prompt", back_populates="main_records")
    deliveries = relationship(
        "Delivery", back_populates="article", cascade="all, delete-orphan"
    )
    category_matches = relationship(
        "MainCategoryMatch",
        back_populates="article",
        cascade="all, delete-orphan",
        order_by="MainCategoryMatch.rank",
    )


class MainCategoryMatch(Base):
    """One stored classification answer for a record (v2-combined).

    The single generation call classifies each thesis into up to
    ``settings.top_n_categories`` ISCED-F 2013 categories; every qualifying
    match (score >= ``settings.min_category_relevance``, exact category name)
    becomes a row here, ranked 1..N by score. ``rank == 1`` is the primary.

    Rows are replaced wholesale on regeneration/manual override. A record
    whose only row is the ``other`` fallback (score 0.0, rank 1,
    reason ``no auto-match``) received no auto-deliveries and waits for a
    reviewer's manual assignment.

    ``model_id``/``prompt_id`` snapshot WHICH configuration classified the
    record; ``deliveries.category_id`` snapshots only the top-1 category for
    audit (a known limitation in the multi-category case).
    """

    __tablename__ = "main_category_matches"
    __table_args__ = (
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="ck_main_category_matches_score"),
        CheckConstraint("rank >= 1 AND rank <= 5", name="ck_main_category_matches_rank"),
        Index("ix_main_cat_matches_main_id", "main_id"),
        Index("ix_main_cat_matches_score", "main_id", "score"),
    )

    id = Column(Integer, primary_key=True)
    main_id = Column(Integer, ForeignKey("main.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    score = Column(Float, nullable=False)
    rank = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    model_id = Column(Integer, ForeignKey("models.id", ondelete="SET NULL"), nullable=True)
    prompt_id = Column(Integer, ForeignKey("prompts.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    article = relationship("Main", back_populates="category_matches")
    category = relationship("Category", back_populates="main_matches")


class Channel(Base):
    """A destination the sender Worker can publish to.

    A channel is a platform plus a destination chat id — deliberately NOT a
    credential. Bot tokens stay in configuration (``app/config.py``), so a
    channel row can be read, joined and audited without exposing a secret.

    ``username`` is rendered as the message footer so readers know which
    channel the article came from.

    ``bot_token_env`` optionally names the environment variable holding THIS
    channel's bot token (never the token itself); when set and present it
    overrides the platform-wide token, so a second channel on the same
    platform needs no code change.
    """

    __tablename__ = "channels"
    __table_args__ = (
        CheckConstraint("platform IN ('bale', 'telegram')", name="ck_channels_platform"),
    )

    id = Column(Integer, primary_key=True)
    platform = Column(String(20), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    username = Column(String(255), nullable=True)
    title = Column(String(255), nullable=True)
    bot_token_env = Column(String(100), nullable=True)
    #: Fernet ciphertext of this channel's own bot token, when it has one.
    bot_token_encrypted = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    #: When this channel started receiving news. Refreshed every time it is
    #: switched ON. The sender only fans an article out to a channel whose
    #: ``activated_at`` is not later than the article's ``created_at``, so a
    #: channel added today never inherits the archive that predates it.
    activated_at = Column(DateTime, nullable=False, server_default=func.now())

    routes = relationship("FieldChannelRoute", back_populates="channel")
    category_routes = relationship("CategoryChannelRoute", back_populates="channel")
    deliveries = relationship("Delivery", back_populates="channel")


class FieldChannelRoute(Base):
    """The one-to-many field -> channel mapping.

    Today every field routes to the single Bale channel, but the mapping is a
    table rather than a constant so splitting fields across Bale/Telegram
    channels later is a row insert and not a code change.
    """

    __tablename__ = "field_channel_routes"

    field_id = Column(Integer, ForeignKey("fields.id", ondelete="CASCADE"), primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    field_record = relationship("Field", back_populates="channel_routes")
    channel = relationship("Channel", back_populates="routes")


class CategoryChannelRoute(Base):
    """The category -> channel mapping: the business routing chain.

    An article reaches a channel through field -> category -> channels, so
    the category (not each of the 500+ fields) is what the owner manages.
    ``field_channel_routes`` stays as the field-level exception table and is
    consulted when a category has no route; unmapped categories fall back to
    all active channels. The permissive seed (every category to every active
    channel) preserves pre-existing publishing behavior until refined.
    """

    __tablename__ = "category_channel_routes"

    category_id = Column(Integer, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    category = relationship("Category", back_populates="channel_routes")
    channel = relationship("Channel", back_populates="category_routes")


class Delivery(Base):
    """The per-(article, channel) send record claimed by the sender Worker.

    ``deliveries`` — not a flag on ``main`` — is what makes "processed but not
    sent" answerable: once a field fans out to several channels an article has
    many independent send states, each with its own attempts and error.

    Status lifecycle (one state machine,
    ``app/services/transitions.py``): ``awaiting_confirmation`` → ``pending``
    → ``sending`` → ``sent``, plus ``failed`` (with ``pending`` after a
    retryable failure or an operator cancel) and ``deleted_from_channel``
    (the operator removed the published message; the row is kept as history).
    ``awaiting_confirmation`` is the gated initial status when
    ``REQUIRE_HUMAN_CONFIRMATION`` is on: approval flips it to ``pending``.

    ``category_id``/``route_source`` snapshot which category and which route
    level (``category`` | ``field`` | ``fallback``) produced the row, so a
    delivery stays explainable after later route edits.

    Unlike the generation Worker, sending IS retried: a transient network or
    provider error should not silently drop a published article. Attempts are
    bounded (``next_attempt_at`` + backoff, then ``failed``) so a permanently
    broken channel cannot block the queue forever.
    """

    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_confirmation', 'pending', 'sending', 'sent', "
            "'failed', 'deleted_from_channel')",
            name="ck_deliveries_status",
        ),
        Index("ix_deliveries_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_deliveries_channel_id", "channel_id"),
    )

    id = Column(Integer, primary_key=True)
    main_id = Column(Integer, ForeignKey("main.id", ondelete="CASCADE"), nullable=False)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    route_source = Column(String(20), nullable=True)
    status = Column(String(30), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    platform_message_id = Column(BigInteger, nullable=True)
    error_message = Column(Text, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=False, server_default=func.now())
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    article = relationship("Main", back_populates="deliveries")
    channel = relationship("Channel", back_populates="deliveries")
    category = relationship("Category")
