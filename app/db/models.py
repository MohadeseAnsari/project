import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models (schema is managed by migrations)."""


class Category(Base):
    """A deterministic field category (e.g. ``پزشکی و علوم سلامت``, ``other``)."""

    __tablename__ = "categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(500), unique=True, nullable=False)
    normalized_name = Column(String(500), unique=True, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    source = Column(String(20), nullable=False, default="spreadsheet")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    category = relationship("Category", back_populates="fields")
    main_records = relationship("Main", back_populates="field_record")


class Model(Base):
    """LLM model configuration; ``is_active`` marks the model the Worker uses."""

    __tablename__ = "models"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)
    provider = Column(String(50), nullable=False)
    version = Column(String(50), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    main_records = relationship("Main", back_populates="model")


class Prompt(Base):
    """A versioned system prompt; ``is_active`` marks the prompt the Worker uses."""

    __tablename__ = "prompts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version = Column(String(50), unique=True, nullable=False)
    system_prompt = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    main_records = relationship("Main", back_populates="prompt")


class Main(Base):
    """The thesis/news record and job-state queue entry.

    Status lifecycle: ``ready`` → ``processing`` → ``review`` | ``failed``.
    ``claimed_at``/``heartbeat_at`` support Worker crash recovery;
    ``field_id``/``category_id``/``model_id``/``prompt_id`` provide
    auditability of the classification and generation configuration used.
    """

    __tablename__ = "main"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready', 'processing', 'review', 'failed')",
            name="ck_main_status",
        ),
        Index("ix_main_status_created_at", "status", "created_at"),
        Index("ix_main_field_id", "field_id"),
        Index("ix_main_category_id", "category_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
