"""Detect obsolete provider results without involving independent user notes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from smartapply.database.models import Application, Job, JobDuplicateStatus, JobStatus
from smartapply.pipeline.errors import StaleTaskResultError, TaskCancelledError


def check_cancelled(stop_requested: Callable[[], bool] | None) -> None:
    if stop_requested is not None and stop_requested():
        raise TaskCancelledError()


class CancellableProvider:
    """Apply cancellation between every provider call, including text repairs."""

    def __init__(self, provider, stop_requested: Callable[[], bool]):
        self._provider = provider
        self._stop_requested = stop_requested

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def complete_json(self, **kwargs):
        check_cancelled(self._stop_requested)
        result = self._provider.complete_json(**kwargs)
        # A validated extraction is an unfinished processing checkpoint. Its
        # caller stores it before checking cancellation, without publishing an
        # analysis or starting the evaluation. This avoids buying it again.
        if kwargs.get("purpose") != "job_offer_analysis":
            check_cancelled(self._stop_requested)
        return result


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def job_snapshot_key(job: Job) -> str:
    """Fingerprint source content, identity and workflow decisions."""
    return _digest({field: getattr(job, field) for field in (
        "external_id", "title", "company", "location", "contract_type", "remote_policy",
        "description", "cleaned_description", "application_url", "apply_options", "source",
        "source_data", "published_date", "canonical_job_id", "possible_duplicate_of_id",
        "duplicate_review_status", "duplicate_match_type", "duplicate_confidence", "status",
        "filtered_at", "ranked_at", "analyzed_at", "archived_at", "shortlisted_at",
        "shortlist_origin",
    )})


def job_is_active(job: Job) -> bool:
    return (
        job.archived_at is None
        and job.status not in {JobStatus.ARCHIVED, JobStatus.SENT, JobStatus.INTERVIEW,
                               JobStatus.REJECTED}
        and job.duplicate_review_status != JobDuplicateStatus.PENDING
    )


def application_snapshot_key(application: Application) -> str:
    # Notes and updated_at are deliberately absent: independent note editing
    # must remain possible while a dossier is generated.
    return _digest({field: getattr(application, field) for field in (
        "id", "job_id", "status", "form_submitted_at", "cv_json", "cv_docx_path",
        "cv_pdf_path", "form_submission_url",
    )})


@dataclass(frozen=True)
class PublicationSnapshot:
    job_key: str
    application_key: str
    analysis_key: str

    @classmethod
    def capture(cls, job: Job, application: Application) -> PublicationSnapshot:
        analysis = job.analysis
        return cls(job_snapshot_key(job), application_snapshot_key(application),
                   _digest({column.name: getattr(analysis, column.name)
                            for column in analysis.__table__.columns}
                           if analysis is not None else None))

    def validate(self, job: Job | None, application: Application | None) -> None:
        if (job is None or application is None or not job_is_active(job)
                or application.form_submitted_at is not None
                or application.status in {JobStatus.SENT, JobStatus.INTERVIEW,
                                          JobStatus.REJECTED, JobStatus.ARCHIVED}
                or self != self.capture(job, application)):
            raise StaleTaskResultError()
