"""Deterministic admission shared by immediate and Batch offer analysis.

Rejection evidence and workflow history decide which offers may be analyzed.
Numeric ranking is independent and never limits this queue.
"""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, load_only, selectinload

from smartapply.database import session_scope, write_session_scope
from smartapply.database.analysis_batches import (
    ACTIVE_ANALYSIS_RUN_STATES,
    AnalysisBatchItem,
    AnalysisBatchRun,
)
from smartapply.database.models import (
    Application,
    Job,
    JobAnalysis,
    JobDuplicateStatus,
    JobScore,
    JobStatus,
)
from smartapply.filtering.hard_reject.types import HardRejectDisposition
from smartapply.filtering.manual_policy import evaluate_or_bypass
from smartapply.jobsearch.offer_state import offer_state
from smartapply.pipeline.process.filter_state import (
    current_hard_reject_disposition,
    has_manual_keep_override,
    is_current_hard_reject_survivor,
)
from smartapply.pipeline.reports import LocalFilterReport
from smartapply.pipeline.snapshots import check_cancelled, job_is_active, job_snapshot_key

_SUBMITTED_STATUSES = {JobStatus.SENT, JobStatus.INTERVIEW}


def analysis_group_available(
    session: Session,
    job_id: int,
    *,
    exclude_applications: bool = True,
) -> bool:
    """Protect applications and unresolved reviews across the offer's descendants."""
    group = select(Job.id).where(Job.id == job_id).cte("analysis_group", recursive=True)
    group = group.union(select(Job.id).join(group, or_(
        Job.canonical_job_id == group.c.id,
        and_(
            Job.duplicate_review_status == JobDuplicateStatus.PENDING,
            Job.possible_duplicate_of_id == group.c.id,
        ),
    )))
    if session.scalar(select(Job.id).where(
        Job.id.in_(select(group.c.id)),
        or_(Job.duplicate_review_status == JobDuplicateStatus.PENDING,
            Job.status.in_(_SUBMITTED_STATUSES)),
    ).limit(1)) is not None:
        return False
    application_conditions = [Application.job_id.in_(select(group.c.id))]
    if not exclude_applications:
        application_conditions.append(or_(
            Application.form_submitted_at.is_not(None),
            Application.status.in_(_SUBMITTED_STATUSES),
        ))
    return session.scalar(select(Application.id).where(*application_conditions).limit(1)) is None


def _unavailable_analysis_groups(session: Session, *, exclude_applications: bool) -> set[int]:
    """Find blocked roots for a whole queue with two queries instead of one per offer."""
    rows = session.execute(select(
        Job.id, Job.canonical_job_id, Job.possible_duplicate_of_id,
        Job.duplicate_review_status, Job.status,
    )).all()
    parents = {job_id: canonical_id for job_id, canonical_id, *_ in rows if canonical_id is not None}

    def root_id(job_id):
        seen = set()
        while job_id in parents and job_id not in seen:
            seen.add(job_id)
            job_id = parents[job_id]
        return int(job_id)

    unavailable = set()
    for job_id, _, possible_id, duplicate_status, status in rows:
        if status in _SUBMITTED_STATUSES:
            unavailable.add(root_id(job_id))
        if duplicate_status == JobDuplicateStatus.PENDING:
            unavailable.add(root_id(job_id))
            if possible_id is not None:
                unavailable.add(root_id(possible_id))
    for job_id, submitted_at, status in session.execute(select(
        Application.job_id, Application.form_submitted_at, Application.status,
    )):
        if exclude_applications or submitted_at is not None or status in _SUBMITTED_STATUSES:
            unavailable.add(root_id(job_id))
    return unavailable


def _has_available_workflow(
    job: Job,
    *,
    include_analyzed: bool,
    exclude_applications: bool,
) -> bool:
    if not job_is_active(job) or job.canonical_job_id is not None:
        return False
    state = offer_state(job)
    if state.completed and not (include_analyzed and state.analyzed):
        return False
    application = job.application
    if application is not None:
        if exclude_applications:
            return False
        if (
            application.form_submitted_at is not None
            or application.status in _SUBMITTED_STATUSES
        ):
            return False
    return True


def is_analysis_candidate(
    job: Job,
    *,
    include_analyzed: bool = False,
    exclude_applications: bool = True,
) -> bool:
    """Require an active canonical offer and a current deterministic keep proof."""
    return (
        _has_available_workflow(
            job,
            include_analyzed=include_analyzed,
            exclude_applications=exclude_applications,
        )
        and job.filtered_at is not None
        and is_current_hard_reject_survivor(
            job.score.components if job.score is not None else None,
        )
    )


def is_current_analysis_candidate(
    session: Session,
    job: Job,
    *,
    include_analyzed: bool = False,
    exclude_applications: bool = True,
    exclude_reserved: bool = False,
) -> bool:
    """Recheck admission and group history before a paid call or publication."""
    return (
        is_analysis_candidate(
            job, include_analyzed=include_analyzed, exclude_applications=exclude_applications,
        )
        and (not exclude_reserved or job.id not in reserved_analysis_job_ids(session))
        and analysis_group_available(session, int(job.id), exclude_applications=exclude_applications)
    )


def analysis_queue_job_ids(session: Session) -> list[int]:
    """Read the eligible queue for counts and launch intents without offer bodies."""
    unavailable = _unavailable_analysis_groups(session, exclude_applications=True)
    unavailable.update(reserved_analysis_job_ids(session))
    jobs = session.scalars(
        select(Job)
        .where(
            Job.archived_at.is_(None),
            Job.analyzed_at.is_(None),
            Job.canonical_job_id.is_(None),
            Job.filtered_at.is_not(None),
        )
        .options(
            load_only(
                Job.id, Job.status, Job.archived_at, Job.analyzed_at,
                Job.canonical_job_id, Job.duplicate_review_status, Job.filtered_at,
            ),
            selectinload(Job.score).load_only(JobScore.components),
            selectinload(Job.analysis).load_only(JobAnalysis.id),
            selectinload(Job.application).load_only(Application.status, Application.form_submitted_at),
        )
        .order_by(Job.id.asc())
    )
    return [int(job.id) for job in jobs if job.id not in unavailable and is_analysis_candidate(job)]


def reserved_analysis_job_ids(session: Session) -> set[int]:
    """Keep active and uncertain Batch submissions out of new analysis intents."""
    return set(session.scalars(
        select(AnalysisBatchItem.job_id)
        .join(AnalysisBatchRun, AnalysisBatchRun.id == AnalysisBatchItem.run_id)
        .where(AnalysisBatchRun.state.in_(ACTIVE_ANALYSIS_RUN_STATES)),
    ))


def has_protected_workflow_history(job: Job) -> bool:
    """Filter repair must preserve recorded analyses, selections and dossiers."""
    components = (
        job.score.components
        if job.score is not None and isinstance(job.score.components, dict)
        else {}
    )
    raw_reasons = components.get("reasons")
    reasons = raw_reasons if isinstance(raw_reasons, (list, tuple)) else ()
    return bool(
        job.analysis is not None
        or job.analyzed_at is not None
        or job.application is not None
        or job.shortlisted_at is not None
        or job.shortlist_origin is not None
        or job.canonical_job_id is not None
        or job.possible_duplicate_of_id is not None
        or (job.duplicate_review_status or JobDuplicateStatus.NONE)
        not in {JobDuplicateStatus.NONE, JobDuplicateStatus.REJECTED}
        or any(str(key).startswith("manual_") for key in components)
        or "reactivation" in components
        or any(str(reason).startswith("Manual override:") for reason in reasons)
        or components.get("rejection_stage") not in {None, "local_filter"}
        or job.status not in {JobStatus.SCRAPED, JobStatus.FILTERED}
    )


def _load_jobs(session, job_ids: list[int] | None = None) -> list[Job]:
    statement = (
        select(Job)
        .where(Job.archived_at.is_(None), Job.canonical_job_id.is_(None))
        .options(selectinload(Job.score), selectinload(Job.analysis), selectinload(Job.application))
        .order_by(Job.id.asc())
    )
    if job_ids is not None:
        statement = statement.where(Job.id.in_(job_ids))
    return list(session.scalars(statement))


class AnalysisAdmissionMixin:
    """Prepare and read the entire deterministic survivor queue without providers."""

    def analysis_candidates(
        self,
        job_ids: list[int] | None = None,
        *,
        include_analyzed: bool = False,
        exclude_applications: bool = True,
    ) -> list[Job]:
        """Return detached offers whose analysis admission is currently valid."""
        with session_scope() as session:
            unavailable = _unavailable_analysis_groups(session, exclude_applications=exclude_applications)
            if exclude_applications:
                unavailable.update(reserved_analysis_job_ids(session))
            candidates = [
                job for job in _load_jobs(session, job_ids)
                if int(job.id) not in unavailable and is_analysis_candidate(
                    job,
                    include_analyzed=include_analyzed,
                    exclude_applications=exclude_applications,
                )
            ]
            session.expunge_all()
        return candidates

    def prepare_analysis(
        self,
        *,
        job_ids: list[int] | None = None,
        local_filter_override_ids: list[int] | None = None,
        include_analyzed: bool = False,
        exclude_applications: bool = True,
    ) -> LocalFilterReport:
        """Repair deterministic filter state before analysis.

        Explicit keep overrides retain their audit. Existing workflow records
        receive metadata repairs only; their analysis, rank and dossier survive.
        A concurrent source or filter change invalidates the prepared result.
        """
        stop_requested = getattr(self, "stop_requested", None)
        check_cancelled(stop_requested)
        override_ids = set(local_filter_override_ids or [])
        with session_scope() as session:
            unavailable = _unavailable_analysis_groups(session, exclude_applications=exclude_applications)
            if exclude_applications:
                unavailable.update(reserved_analysis_job_ids(session))
            jobs = [
                job for job in _load_jobs(session, job_ids)
                if int(job.id) not in unavailable and _has_available_workflow(
                    job,
                    include_analyzed=include_analyzed,
                    exclude_applications=exclude_applications,
                )
            ]
            prepared = []
            for job in jobs:
                components = job.score.components if job.score is not None else None
                disposition = current_hard_reject_disposition(components)
                force_keep = int(job.id) in override_ids or has_manual_keep_override(components)
                if (
                    job.filtered_at is None
                    or disposition is None
                    or (disposition is HardRejectDisposition.REJECTED and force_keep)
                ):
                    prepared.append((job, job_snapshot_key(job), components, force_keep))
            session.expunge_all()

        results = []
        for job, snapshot, components, force_keep in prepared:
            check_cancelled(stop_requested)
            results.append((
                int(job.id), snapshot, components, force_keep, evaluate_or_bypass(self.filter, job),
            ))
        check_cancelled(stop_requested)
        with write_session_scope() as session:
            reserved = (
                reserved_analysis_job_ids(session)
                if exclude_applications else set()
            )
            current_by_id = {int(job.id): job for job in _load_jobs(session, [r[0] for r in results])}
            for job_id, snapshot, old_components, force_keep, result in results:
                job = current_by_id.get(job_id)
                if job is None or job_id in reserved or not _has_available_workflow(
                    job,
                    include_analyzed=include_analyzed,
                    exclude_applications=exclude_applications,
                ):
                    continue
                if not analysis_group_available(session, job_id, exclude_applications=exclude_applications):
                    continue
                components = job.score.components if job.score is not None else None
                if job_snapshot_key(job) != snapshot or components != old_components:
                    continue
                if has_protected_workflow_history(job):
                    self._persist_filter_state_only(session, job, result, force_keep=force_keep)
                else:
                    self._persist_filter_result(
                        session, job, result, force_keep=force_keep, update_pipeline_status=True,
                    )

        candidates = self.analysis_candidates(
            job_ids,
            include_analyzed=include_analyzed,
            exclude_applications=exclude_applications,
        )
        kept_ids = [int(job.id) for job in candidates]
        kept_set = set(kept_ids)
        with session_scope() as session:
            rejected_ids = [
                int(job.id) for job in session.scalars(
                    select(Job).where(Job.id.in_([int(job.id) for job in jobs])),
                )
                if int(job.id) not in kept_set
                and current_hard_reject_disposition(
                    job.score.components if job.score is not None else None,
                ) is HardRejectDisposition.REJECTED
            ]
        return LocalFilterReport(
            total=len(jobs),
            kept=len(kept_ids),
            rejected=len(rejected_ids),
            duplicates_removed=0,
            kept_ids=kept_ids,
            rejected_ids=rejected_ids,
            pending_relevance=len(kept_ids),
            pending_relevance_ids=kept_ids,
        )


__all__ = [
    "AnalysisAdmissionMixin",
    "analysis_group_available",
    "analysis_queue_job_ids",
    "has_protected_workflow_history",
    "is_analysis_candidate",
    "is_current_analysis_candidate",
    "reserved_analysis_job_ids",
]
