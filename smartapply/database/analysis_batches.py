"""Durable execution journal for the two dependent analysis batches.

These states describe processing, never offer classifications. Completed offer
analyses still live in ``job_analyses``; intermediate results belong to a run
only so interrupted processing can resume without buying successful calls again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from smartapply.database.models import Base, _utcnow

# These runs still reserve their offers. A blocked run waits for an explicit
# retry and must never submit a second copy of those offers automatically.
ACTIVE_ANALYSIS_RUN_STATES = ("active", "attention", "blocked")


class AnalysisBatchRun(Base):
    __tablename__ = "analysis_batch_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    model: Mapped[str] = mapped_column(String(128))
    profile_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    fit_context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisBatchItem(Base):
    __tablename__ = "analysis_batch_items"
    __table_args__ = (UniqueConstraint("run_id", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_batch_runs.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_snapshot: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text)
    analyzer_input: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="extraction_pending", index=True)
    offer_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str] = mapped_column(Text, default="")


class AnalysisBatchWave(Base):
    __tablename__ = "analysis_batch_waves"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_batch_runs.id"), index=True)
    phase: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32), default="prepared")
    input_file_id: Mapped[str | None] = mapped_column(String(128))
    provider_batch_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    # Each unique prompt has one custom ID and may serve several exact copies.
    requests: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    consumed_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
