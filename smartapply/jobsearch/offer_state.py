"""Canonical offer progress, derived from persisted workflow evidence.

The deterministic filter routes unfinished offers. Its historical result is
not a current classification once analysis or document generation is complete.
This module owns that distinction for queues, presentation and persistence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import JSON, func, or_

from smartapply.database.models import Application, Job, JobDuplicateStatus, JobScore, JobStatus
from smartapply.jobsearch.status import display_status

COMPLETED_STATUSES = frozenset({
    JobStatus.ANALYZED, JobStatus.SHORTLISTED, JobStatus.READY_FOR_FORM_SUBMISSION,
    JobStatus.QUALITY_REJECTED, JobStatus.SENT, JobStatus.INTERVIEW,
})
APPLICATION_TRACKING_STATUSES = COMPLETED_STATUSES - {JobStatus.ANALYZED, JobStatus.SHORTLISTED}
FILTER_HISTORY_KEY = "completed_filter_routing"
STALE_RESERVATION_AFTER = timedelta(minutes=30)


def application_has_documents(application: Application) -> bool:
    return bool(application.documents or application.cv_docx_path
                or application.cv_pdf_path or application.cv_json)


def reservation_is_stale(application: Application) -> bool:
    """An empty reservation becomes reusable after thirty minutes."""
    if application_has_documents(application):
        return False
    timestamp = application.updated_at or application.created_at
    if timestamp is None:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timestamp >= STALE_RESERVATION_AFTER


def is_reusable_reservation(application: Application) -> bool:
    return bool(application.status == JobStatus.ANALYZED
                and application.form_submitted_at is None and reservation_is_stale(application))


def recorded_filter_disposition(components: object) -> str | None:
    """Read the deterministic proof, including history for explicit reanalysis."""
    if not isinstance(components, Mapping):
        return None
    value = components.get("filter_disposition")
    if value is None:
        history = components.get(FILTER_HISTORY_KEY)
        value = history.get("disposition") if isinstance(history, Mapping) else None
    return value if isinstance(value, str) else None


def has_manual_rescue(components: object) -> bool:
    """Recognize an explicit, persisted restoration of an archived offer."""
    if not isinstance(components, Mapping):
        return False
    rescue = components.get("manual_rescue")
    return bool(
        isinstance(rescue, Mapping)
        and rescue.get("schema_version") == 3
        and isinstance(rescue.get("rescued_at"), str)
        and rescue["rescued_at"].strip()
    )


def rescued_duplicate_review_expression():
    """SQL counterpart of restored review; an old dossier remains archived."""
    return (
        (
            (func.coalesce(Job.duplicate_review_status, JobDuplicateStatus.NONE) == JobDuplicateStatus.PENDING)
            | (
                (func.coalesce(func.json_type(
                    JobScore.components, "$.manual_rescue.duplicate_review_job_id"
                ), "") == "integer")
                & (func.coalesce(func.json_extract(
                    JobScore.components, "$.manual_rescue.duplicate_review_job_id"
                ), 0) > 0)
            )
        )
        & Job.archived_at.is_(None)
        & (Job.status != JobStatus.ARCHIVED)
        & (func.coalesce(func.json_extract(
            JobScore.components, "$.manual_rescue.schema_version"
        ), 0) == 3)
        & (func.coalesce(func.json_type(
            JobScore.components, "$.manual_rescue.rescued_at"
        ), "") == "text")
        & (func.length(func.trim(func.coalesce(func.json_extract(
            JobScore.components, "$.manual_rescue.rescued_at"
        ), ""))) > 0)
    )


@dataclass(frozen=True)
class OfferState:
    workflow_status: str
    status: str
    analyzed: bool
    generated: bool
    completed: bool
    filter_disposition: str

    @property
    def awaits_classification(self) -> bool:
        return self.filter_disposition == "pending_relevance"


def offer_state(job: Job, *, components: object = None) -> OfferState:
    """Resolve progress with archive, tracking and completed work taking priority.

    A duplicate relationship stays available as evidence, but cannot make a
    completed offer look new. Missing timestamps never erase a saved analysis.
    An empty generation reservation is not a completed dossier.
    """
    application = job.application
    if components is None:
        components = job.score.components if job.score is not None else None
    rescue = components.get("manual_rescue") if isinstance(components, Mapping) else None
    review_id = rescue.get("duplicate_review_job_id") if isinstance(rescue, Mapping) else None
    reviewing_rescue = (
        (job.duplicate_review_status == JobDuplicateStatus.PENDING
         or (type(review_id) is int and review_id > 0))
        and job.archived_at is None and job.status != JobStatus.ARCHIVED
        and has_manual_rescue(components)
    )
    generated = application is not None and application_has_documents(application)
    analyzed = job.analyzed_at is not None or job.analysis is not None
    tracked = application is not None and (
        application.form_submitted_at is not None
        or application.status in APPLICATION_TRACKING_STATUSES
    )
    completed = analyzed or generated or tracked or job.status in COMPLETED_STATUSES
    archived = bool(job.archived_at or job.status == JobStatus.ARCHIVED
                    or (application and application.status == JobStatus.ARCHIVED
                        and not reviewing_rescue))
    if archived:
        workflow = JobStatus.ARCHIVED
    elif reviewing_rescue:
        workflow = JobStatus.DUPLICATE_REVIEW
    elif application and application.status == JobStatus.INTERVIEW:
        workflow = application.status
    elif application and (application.form_submitted_at is not None
                          or application.status == JobStatus.SENT):
        workflow = JobStatus.SENT
    elif application and application.status in APPLICATION_TRACKING_STATUSES:
        workflow = display_status(application.status)
    elif generated:
        workflow = JobStatus.READY_FOR_FORM_SUBMISSION
    elif analyzed:
        workflow = JobStatus.SHORTLISTED if job.shortlisted_at else JobStatus.ANALYZED
    elif job.status in COMPLETED_STATUSES:
        workflow = display_status(job.status)
    else:
        workflow = JobStatus.FILTERED if job.filtered_at else JobStatus.SCRAPED
    status = display_status(workflow)
    if not completed and not archived and job.duplicate_review_status == JobDuplicateStatus.PENDING:
        status = JobStatus.DUPLICATE_REVIEW
    raw = components.get("filter_disposition") if isinstance(components, Mapping) else None
    disposition = ""
    if not completed and (not archived or raw == "rejected"):
        disposition = "rejected" if raw == "rejected" else "pending_relevance"
    return OfferState(workflow, status, analyzed, generated, bool(completed), disposition)


def completed_offer_expression():
    """SQL equivalent of completed evidence for queues that load metadata only."""
    return or_(
        Job.analyzed_at.is_not(None), Job.analysis.has(), Job.status.in_(COMPLETED_STATUSES),
        Job.application.has(or_(
            Application.form_submitted_at.is_not(None),
            Application.status.in_(APPLICATION_TRACKING_STATUSES),
            Application.documents.any(),
            Application.cv_docx_path.is_not(None) & (Application.cv_docx_path != ""),
            Application.cv_pdf_path.is_not(None) & (Application.cv_pdf_path != ""),
            Application.cv_json.is_not(None) & (Application.cv_json != JSON.NULL)
            & (Application.cv_json != {}),
        )),
    )
