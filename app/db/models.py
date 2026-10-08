from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
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
    """A deterministic field category (e.g. ``پزشکی و علوم سلامت``, ``other``)."""

    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    fields = relationship("Field", back_populates="category")
    main_records = relationship("Main", back_populates="category")


class Field(Base):
    """A known thesis field.

    ``normalized_name`` is the stable identity used for exact matching and
    duplicate prevention;     ``source`` distinguishes spreadsheet-seeded fields
    from runtime-discovered ones.
    """

    __tablename__ = "fields"

    id = Column(Integer, primary_key=True)
    name = Column(String(500), unique=True, nullable=False)
    normalized_name = Column(String(500), unique=True, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    source = Column(String(20), nullable=False, default="spreadsheet")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    category = relationship("Category", back_populates="fields")
    main_records = relationship("Main", back_populates="field_record")
    channel_routes = relationship(
        "FieldChannelRoute", back_populates="field_record", cascade="all, delete-orphan"
    )


class Model(Base):
    """LLM model configuration; ``is_active`` marks the model the Worker uses."""

    __tablename__ = "models"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    provider = Column(String(50), nullable=False)
    version = Column(String(50), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

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


class Main(Base):
    """The thesis/news record and job-state queue entry.

    Status lifecycle: ``ready`` → ``processing`` → ``review`` | ``failed``,
    plus ``rejected`` for an explicit human rejection from ``review``.
    ``claimed_at``/``heartbeat_at`` support Worker crash recovery;
    ``field_id``/``category_id``/``model_id``/``prompt_id`` provide
    auditability of the classification and generation configuration used.
    """

    __tablename__ = "main"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready', 'processing', 'review', 'failed', 'rejected')",
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
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    field_record = relationship("Field", back_populates="main_records")
    category = relationship("Category", back_populates="main_records")
    model = relationship("Model", back_populates="main_records")
    prompt = relationship("Prompt", back_populates="main_records")
    deliveries = relationship(
        "Delivery", back_populates="article", cascade="all, delete-orphan"
    )


class Channel(Base):
    """A destination the sender Worker can publish to.

    A channel is a platform plus a destination chat id — deliberately NOT a
    credential. Bot tokens stay in configuration (``app/config.py``), so a
    channel row can be read, joined and audited without exposing a secret.

    ``username`` is rendered as the message footer so readers know which
    channel the article came from.
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
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    routes = relationship("FieldChannelRoute", back_populates="channel")
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


class Delivery(Base):
    """The per-(article, channel) send record claimed by the sender Worker.

    ``deliveries`` — not a flag on ``main`` — is what makes "processed but not
    sent" answerable: once a field fans out to several channels an article has
    many independent send states, each with its own attempts and error.

    Status lifecycle: ``pending`` → ``sending`` → ``sent`` | ``failed``, with
    ``pending`` returned after a retryable failure. ``awaiting_confirmation`` is
    reserved for the planned human-in-the-loop step and is not written yet.

    Unlike the generation Worker, sending IS retried: a transient network or
    provider error should not silently drop a published article. Attempts are
    bounded (``next_attempt_at`` + backoff, then ``failed``) so a permanently
    broken channel cannot block the queue forever.
    """

    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'awaiting_confirmation')",
            name="ck_deliveries_status",
        ),
        Index("ix_deliveries_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_deliveries_channel_id", "channel_id"),
    )

    id = Column(Integer, primary_key=True)
    main_id = Column(Integer, ForeignKey("main.id", ondelete="CASCADE"), nullable=False)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
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
