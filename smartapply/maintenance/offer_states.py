"""Idempotent repair using the same offer-state policy as the live workflow."""

from sqlalchemy import select, text
from sqlalchemy.orm import load_only, selectinload

from smartapply.database import write_session_scope
from smartapply.database.models import Application, GeneratedDocument, Job, JobAnalysis, JobScore
from smartapply.database.repository.offer_state import synchronize_offer_state


def reconcile_offer_states(*, stop_requested=None) -> list[dict]:
    changes = []
    with write_session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        jobs = session.scalars(select(Job).options(
            load_only(Job.id, Job.status, Job.archived_at, Job.analyzed_at,
                      Job.shortlisted_at, Job.filtered_at, Job.duplicate_review_status),
            selectinload(Job.score).load_only(JobScore.components),
            selectinload(Job.analysis).load_only(JobAnalysis.created_at),
            selectinload(Job.application).load_only(
                Application.status, Application.form_submitted_at, Application.cv_json,
                Application.cv_docx_path, Application.cv_pdf_path,
            ).selectinload(Application.documents).load_only(GeneratedDocument.id),
        )).all()
        for job in jobs:
            if stop_requested and stop_requested():
                session.rollback()
                return []
            before = {
                "status": job.status,
                "analyzed_at": str(job.analyzed_at) if job.analyzed_at else None,
                "filter_disposition": (job.score.components or {}).get("filter_disposition")
                if job.score and isinstance(job.score.components, dict) else None,
            }
            if synchronize_offer_state(job):
                changes.append({"job_id": job.id, "before": before, "after": {
                    "status": job.status,
                    "analyzed_at": str(job.analyzed_at) if job.analyzed_at else None,
                    "filter_disposition": (job.score.components or {}).get("filter_disposition")
                    if job.score and isinstance(job.score.components, dict) else None,
                }})
    return changes
