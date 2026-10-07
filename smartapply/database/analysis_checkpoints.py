"""Validated extraction awaiting evaluation, independent of cache expiry.

This is a processing checkpoint, not another classification or a permanent
extraction cache. Publication of a complete analysis removes it.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from smartapply.database.models import Base, _utcnow


class AnalysisCheckpoint(Base):
    __tablename__ = "analysis_checkpoints"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    extraction_key: Mapped[str] = mapped_column(String(64))
    offer_data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
