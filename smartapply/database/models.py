"""SQLAlchemy models for Élan's local persistence layer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class JobStatus:
    SCRAPED = "scraped"
    FILTERED = "filtered"
    SHORTLISTED = "shortlisted"
    ANALYZED = "analyzed"
    SENT = "sent"
    INTERVIEW = "interview"
    QUALITY_REJECTED = "quality_rejected"
    READY_FOR_FORM_SUBMISSION = "ready_for_form_submission"
    ARCHIVED = "archived"
    DUPLICATE_REVIEW = "duplicate_review"


class JobDuplicateStatus:
    """Human-review state for offers that may represent the same posting."""

    NONE = "none"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ShortlistOrigin:
    AUTOMATIC = "automatic"
    MANUAL = "manual"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Company(Base):
    """Prospecting companies, independently of scraped offers and applications."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    name_key: Mapped[str] = mapped_column(String(255), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    priority_label: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(255), default="")
    company_type: Mapped[str] = mapped_column(String(255), default="")
    english_level: Mapped[str] = mapped_column(String(255), default="")
    selectivity: Mapped[str] = mapped_column(String(255), default="")
    ranking_reason: Mapped[str] = mapped_column(Text, default="")
    checked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_scraped_at", "status", "scraped_at"),
        Index("ix_jobs_company", "source_company"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    source_company: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contract_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    remote_policy: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    cleaned_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_metadata: Mapped[Any | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    application_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    apply_options: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    source_data: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    # A source listing can be kept as an alias of another persisted offer
    # after a duplicate is confirmed.  Pending matches deliberately keep a
    # null canonical_job_id until the user decides.
    canonical_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    possible_duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    duplicate_review_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=JobDuplicateStatus.NONE, index=True
    )
    duplicate_match_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    duplicate_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    published_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    # Per-phase completion timestamps drive idempotent pipeline routing.
    # The ``status`` enum is kept as a denormalized view for UI/lifecycle —
    # it no longer drives "what to process next".
    filtered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ranked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    shortlisted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    shortlist_origin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(50), default=JobStatus.SCRAPED, index=True)

    score: Mapped[JobScore | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    analysis: Mapped[JobAnalysis | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    application: Mapped[Application | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )


class JobScore(Base):
    __tablename__ = "job_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    components: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="score")


class JobAnalysis(Base):
    __tablename__ = "job_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    role_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(50), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(100), nullable=True)
    main_tasks: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    required_skills: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    nice_to_have: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    match_reasons: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    risks: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    cv_keywords_to_include: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    fit_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    raw_response: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    fit_diagnostics: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="analysis")


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (Index("ix_applications_status_updated_at", "status", "updated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(50), default=JobStatus.ANALYZED, index=True)
    cv_docx_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    cv_pdf_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    cv_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    validation_warnings: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_submission_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    form_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True
    )

    job: Mapped[Job] = relationship(back_populates="application")
    documents: Mapped[list[GeneratedDocument]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(String(50), index=True)
    path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    application: Mapped[Application] = relationship(back_populates="documents")


class LLMCache(Base):
    __tablename__ = "llm_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    model: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(32), default="openai", server_default="openai")
    purpose: Mapped[str | None] = mapped_column(String(100), nullable=True)
    response: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(100), index=True)
    model: Mapped[str] = mapped_column(String(100))
    requested_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="openai", server_default="openai")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    # Immutable attribution survives hard deletion of a Job row.
    job_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Enables targeted removal of exact responses when an offer is deleted.
    cache_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DuplicateReviewEvent(Base):
    """Reversible, transaction-bound evidence for automatic relation decisions."""

    __tablename__ = "duplicate_review_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    reference_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    policy_version: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(100))
    before_state: Mapped[dict[str, Any]] = mapped_column(JSON)
    after_state: Mapped[dict[str, Any]] = mapped_column(JSON)
    history_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


@event.listens_for(Job, "before_insert")
def _insert_display_metadata(_mapper, _connection, job: Job) -> None:
    from smartapply.database.display_metadata import build_display_metadata

    job.display_metadata = build_display_metadata(job.description, job.cleaned_description)


@event.listens_for(Job, "before_update")
def _update_display_metadata(_mapper, _connection, job: Job) -> None:
    if any(
        inspect(job).attrs[name].history.has_changes()
        for name in ("description", "cleaned_description")
    ):
        from smartapply.database.display_metadata import build_display_metadata

        job.display_metadata = build_display_metadata(job.description, job.cleaned_description)
