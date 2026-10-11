"""Durable document generation runs, replay checkpoints and provider submissions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from smartapply.database.models import Base, _utcnow

ACTIVE_GENERATION_RUN_STATES = ("active", "attention", "blocked", "paused")
RESERVED_GENERATION_STATES = (*ACTIVE_GENERATION_RUN_STATES, "partial", "failed")
POLLING_GENERATION_STATES = ("active", "attention")
TERMINAL_GENERATION_ITEM_STATES = ("completed", "stale", "transferred")


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    profile_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    routes: Mapped[dict[str, Any]] = mapped_column(JSON)
    reference_date: Mapped[str] = mapped_column(String(32))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GenerationItem(Base):
    __tablename__ = "generation_items"
    __table_args__ = (UniqueConstraint("run_id", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("generation_runs.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_snapshot: Mapped[str] = mapped_column(String(64))
    analysis_snapshot: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str] = mapped_column(Text, default="")


class GenerationCall(Base):
    __tablename__ = "generation_calls"
    __table_args__ = (UniqueConstraint("item_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("generation_items.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(64))
    schema_name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))
    request_body: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="prepared", index=True)
    response_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str] = mapped_column(Text, default="")
    wave_id: Mapped[str | None] = mapped_column(ForeignKey("generation_waves.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)


class GenerationItemContext(Base):
    """Original execution inputs retained when an offer changes run or mode."""

    __tablename__ = "generation_item_contexts"

    item_id: Mapped[int] = mapped_column(ForeignKey("generation_items.id"), primary_key=True)
    profile_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    routes: Mapped[dict[str, Any]] = mapped_column(JSON)
    reference_date: Mapped[str] = mapped_column(String(32))


class GenerationWave(Base):
    __tablename__ = "generation_waves"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("generation_runs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="prepared", index=True)
    input_file_id: Mapped[str | None] = mapped_column(String(128))
    provider_batch_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    requests: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    consumed_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
