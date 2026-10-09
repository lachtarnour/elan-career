"""A completed prefilter attempt, reusable independently of extraction or fit."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from smartapply.database.models import Base, _utcnow


class ScopeCheckpoint(Base):
    __tablename__ = "scope_checkpoints"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    screening_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    result_data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
