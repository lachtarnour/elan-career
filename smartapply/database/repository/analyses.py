"""Job-analysis repository helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from smartapply.database.models import JobAnalysis


def set_analysis(session: Session, job_id: int, **fields: Any) -> JobAnalysis:
    existing = session.execute(
        select(JobAnalysis).where(JobAnalysis.job_id == job_id)
    ).scalar_one_or_none()
    if existing is None:
        analysis = JobAnalysis(job_id=job_id, **fields)
        session.add(analysis)
        return analysis
    if {"fit_score", "fit_diagnostics", "raw_response"} & fields.keys():
        from smartapply.database.fit_history import archive_fit

        archive_fit(session, existing)
    for key, value in fields.items():
        setattr(existing, key, value)
    return existing
