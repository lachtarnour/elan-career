"""Job repository helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from smartapply.database.models import Job, JobDuplicateStatus, JobScore, JobStatus, ShortlistOrigin
from smartapply.jobsearch.archive_reasons import ARCHIVE_ORIGINS, ArchiveOrigin

_SCORE_COMPONENT_KEYS = frozenset(
    {
        "rule_based",
        "semantic",
        "skills",
        "title",
        "seniority",
        "location",
        "domain",
        "final",
        "cap",
    }
)
_ACTIVE_REJECTION_KEYS = frozenset(
    {"rejection_stage", "rejection_reasons", "rejection_summary", "reasons", "hard_reject", "archive_origin"}
)


def upsert_job(session: Session, *, external_id: str, **fields: Any) -> Job:
    """Insert or update a job by external_id. Returns the persisted instance."""
    job = session.execute(select(Job).where(Job.external_id == external_id)).scalar_one_or_none()
    if job is None:
        job = Job(external_id=external_id, **fields)
        session.add(job)
        session.flush()
        return job
    for key, value in fields.items():
        if value is not None:
            setattr(job, key, value)
    return job


def get_job_by_external_id(session: Session, external_id: str) -> Job | None:
    return session.execute(select(Job).where(Job.external_id == external_id)).scalar_one_or_none()


def get_known_external_ids(session: Session, source: str) -> set[str]:
    """Return every ``external_id`` already persisted for ``source``."""
    rows = session.execute(select(Job.external_id).where(Job.source == source)).scalars()
    return {row for row in rows if row}


def list_known_jobs(session: Session) -> Sequence[Job]:
    """Return every job already persisted in the database."""
    return session.execute(select(Job)).scalars().all()


def list_jobs(
    session: Session,
    *,
    status: str | None = None,
    source: str | None = None,
    limit: int | None = None,
) -> Sequence[Job]:
    stmt = select(Job)
    if status:
        stmt = stmt.where(Job.status == status)
    if source:
        stmt = stmt.where(Job.source == source)
    stmt = stmt.order_by(Job.scraped_at.desc())
    if limit:
        stmt = stmt.limit(limit)
    return session.execute(stmt).scalars().all()


def update_status(session: Session, job_id: int, status: str) -> None:
    job = session.get(Job, job_id)
    if job is not None:
        job.status = status
        from smartapply.database.repository.offer_state import retire_filter_routing

        retire_filter_routing(job)


def list_pending_processing(session: Session) -> Sequence[Job]:
    """All non-archived jobs that still have work left in the pipeline."""
    stmt = (
        select(Job)
        .where(Job.archived_at.is_(None))
        .where(Job.analyzed_at.is_(None))
        .where(
            or_(
                Job.duplicate_review_status.is_(None),
                Job.duplicate_review_status != JobDuplicateStatus.PENDING,
            )
        )
        .order_by(Job.scraped_at.desc())
    )
    return session.execute(stmt).scalars().all()


def mark_filtered(session: Session, job_id: int) -> None:
    job = session.get(Job, job_id)
    if job is not None:
        job.filtered_at = datetime.now(timezone.utc)


def set_shortlisted(
    session: Session,
    job_id: int,
    *,
    selected: bool,
    origin: str | None = None,
) -> Job | None:
    """Persist a Top selection as the offer's canonical status."""
    job = session.get(Job, job_id)
    if (
        job is None
        or job.archived_at is not None
        or job.duplicate_review_status == JobDuplicateStatus.PENDING
    ):
        return None

    if selected:
        # A Top selection needs a completed filter and AI fit assessment.
        if (
            job.filtered_at is None
            or job.analyzed_at is None
            or job.analysis is None
            or job.analysis.fit_score is None
        ):
            return None
        now = datetime.now(timezone.utc)
        job.shortlisted_at = job.shortlisted_at or now
        if origin == ShortlistOrigin.MANUAL or job.shortlist_origin != ShortlistOrigin.MANUAL:
            job.shortlist_origin = origin or ShortlistOrigin.AUTOMATIC
        # Shortlisting is a first-class offer state. Application tracking is
        # stored on Application and must not turn this into two UI labels.
        job.status = JobStatus.SHORTLISTED
        return job

    job.shortlisted_at = None
    job.shortlist_origin = None
    if job.status == JobStatus.SHORTLISTED:
        # Removing an offer from the generation Top selection returns it to
        # the correct previous workflow state. Analyzed offers must remain
        # visibly analyzed so the dashboard and status filter stay aligned.
        job.status = JobStatus.ANALYZED if job.analyzed_at is not None else JobStatus.FILTERED
    return job


def mark_analyzed(session: Session, job_id: int) -> None:
    job = session.get(Job, job_id)
    if job is not None:
        job.analyzed_at = datetime.now(timezone.utc)
        from smartapply.database.repository.offer_state import retire_filter_routing

        retire_filter_routing(job)


def mark_archived(
    session: Session, job_id: int, *, origin: ArchiveOrigin | None = None,
) -> None:
    """Mark a job archived in both routing and display state."""
    job = session.get(Job, job_id)
    if job is not None:
        if origin is not None:
            if origin not in ARCHIVE_ORIGINS:
                raise ValueError(f"Unsupported archive origin: {origin}")
            from smartapply.database.repository.scores import set_score

            previous = dict(job.score.components) if job.score and job.score.components else {}
            set_score(session, job_id, components={**previous, "archive_origin": origin})
        job.archived_at = datetime.now(timezone.utc)
        # Archiving removes the offer from Top sélection as well. Keeping the
        # old marker here makes an archived offer look shortlisted to any
        # consumer that reads the raw lifecycle fields.
        job.shortlisted_at = None
        job.shortlist_origin = None
        job.status = JobStatus.ARCHIVED
        from smartapply.database.repository.offer_state import retire_filter_routing

        retire_filter_routing(job)


def rescue_archived_job(
    session: Session,
    job_id: int,
    *,
    justification: str | None = None,
) -> Job | None:
    """Restore an archived job as unranked and pending relevance.

    A manual rescue is an auditable routing override.  It must not fabricate a
    perfect matching score or erase the deterministic-rejection evidence that
    motivated the review.
    """
    from smartapply.database.repository.scores import set_score

    job = session.get(Job, job_id)
    if job is None:
        return None

    now = datetime.now(timezone.utc)
    previous_score = session.execute(
        select(JobScore).where(JobScore.job_id == job_id)
    ).scalar_one_or_none()
    previous_components: dict[str, Any] = (
        dict(previous_score.components)
        if previous_score is not None and previous_score.components
        else {}
    )

    job.archived_at = None
    job.analyzed_at = None
    job.filtered_at = now
    job.ranked_at = None
    job.shortlisted_at = None
    job.shortlist_origin = None
    job.status = JobStatus.FILTERED

    previous_rejection = {
        "stage": previous_components.get("rejection_stage"),
        "reasons": list(previous_components.get("rejection_reasons", []) or []),
        "summary": previous_components.get("rejection_summary"),
        "archive_origin": previous_components.get("archive_origin"),
    }
    components = {
        key: value
        for key, value in previous_components.items()
        if key not in _SCORE_COMPONENT_KEYS | _ACTIVE_REJECTION_KEYS
    }
    components["filter_disposition"] = "pending_relevance"
    components["manual_rescue"] = {
        "schema_version": 3,
        "rescued_at": now.isoformat(),
        "justification": (justification or "").strip(),
        "previous_rejection": previous_rejection,
        "previous_hard_reject": previous_components.get("hard_reject"),
    }
    if job.canonical_job_id is not None or job.possible_duplicate_of_id is not None:
        components["manual_rescue"]["previous_duplicate"] = {
            "canonical_job_id": job.canonical_job_id,
            "possible_duplicate_of_id": job.possible_duplicate_of_id,
            "duplicate_review_status": job.duplicate_review_status,
            "duplicate_match_type": job.duplicate_match_type,
            "duplicate_confidence": job.duplicate_confidence,
        }
    job.score = set_score(
        session,
        job_id,
        components=components,
    )
    return job
