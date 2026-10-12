"""Application repository helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from smartapply.database.models import Application, JobStatus


def clear_shortlist_for_sent_applications(session: Session) -> int:
    """Remove stale Top markers from applications already marked as sent."""
    applications = (
        session.execute(
            select(Application)
            .options(joinedload(Application.job))
            .where(Application.status == JobStatus.SENT)
        )
        .scalars()
        .all()
    )
    updated = 0
    for application in applications:
        job = application.job
        if job is not None and (
            job.shortlisted_at is not None or job.status == JobStatus.SHORTLISTED
        ):
            job.shortlisted_at = None
            job.shortlist_origin = None
            job.status = JobStatus.SENT
            updated += 1
    return updated


def create_or_get_application(session: Session, job_id: int) -> Application:
    existing = session.execute(
        select(Application).where(Application.job_id == job_id)
    ).scalar_one_or_none()
    if existing:
        return existing
    app = Application(job_id=job_id, status=JobStatus.ANALYZED)
    session.add(app)
    session.flush()
    return app


def list_applications(
    session: Session,
    *,
    status: str | None = None,
) -> Sequence[Application]:
    stmt = select(Application).order_by(Application.updated_at.desc())
    if status:
        stmt = stmt.where(Application.status == status)
    return session.execute(stmt).scalars().all()


def update_application_tracking(
    session: Session,
    application_id: int,
    *,
    status: str | None = None,
    notes: str | None = None,
    form_submitted: bool = False,
) -> Application:
    """Update the human follow-up state for one application."""
    app = session.get(Application, application_id)
    if app is None:
        raise ValueError(f"Application {application_id} not found")

    job = app.job
    if status is not None:
        app.status = status
    if notes is not None:
        app.notes = notes

    now = datetime.now(timezone.utc)
    if form_submitted:
        app.form_submitted_at = now
        app.status = JobStatus.SENT

    if job is not None and (status is not None or form_submitted):
        # Keep a shortlist marker only while the offer is waiting for dossier
        # generation. Once a candidature is sent, the application is the
        # canonical workflow state.
        if app.status == JobStatus.SENT:
            job.shortlisted_at = None
            job.shortlist_origin = None
        if app.status == JobStatus.ARCHIVED and status is not None:
            from smartapply.database.repository.jobs import mark_archived

            mark_archived(session, job.id, origin="manual")
            return app
        from smartapply.database.repository.offer_state import synchronize_offer_state

        synchronize_offer_state(job)
    return app
