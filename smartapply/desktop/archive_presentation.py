"""Present recorded archive causes and the provenance of inherited dossiers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from smartapply.database.models import Application, Job, JobDuplicateStatus, JobStatus
from smartapply.database.repository.duplicates import canonical_job
from smartapply.jobsearch.archive_reasons import (
    archive_origin_from_components,
    archive_reason_labels,
    archive_rejection_reasons,
    inherited_archive_reason_labels,
)


def _same_archive_event(source: Job, application: Application) -> bool:
    """Legacy UI writes dossier status and offer archive time in one transaction."""
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)

    if source.archived_at is None or application.created_at is None or application.updated_at is None:
        return False
    archived_at = utc(source.archived_at)
    return (
        utc(application.created_at) <= archived_at
        and abs((archived_at - utc(application.updated_at)).total_seconds()) <= 1
        and (application.form_submitted_at is None or utc(application.form_submitted_at) <= archived_at)
    )


def legacy_archive_source(session: Session, job: Job) -> Job | None:
    """Find one documented alias whose archive event belongs to this dossier."""
    application = job.application
    if (
        job.canonical_job_id is not None
        or application is None
        or application.status != JobStatus.ARCHIVED
        or not application.form_submission_url
    ):
        return None
    sources = session.scalars(
        select(Job).where(
            Job.canonical_job_id == job.id,
            Job.duplicate_review_status == JobDuplicateStatus.CONFIRMED,
            Job.application_url == application.form_submission_url,
        ).options(joinedload(Job.score))
    )
    eligible = []
    for source in sources:
        components = source.score.components if source.score else {}
        if _same_archive_event(source, application) and (
            archive_rejection_reasons(components)
            or archive_origin_from_components(components) is not None
        ):
            eligible.append(source)
    return eligible[0] if len(eligible) == 1 else None


def job_archive_reasons(session: Session, job: Job, *, archived: bool) -> tuple[str, ...]:
    """Keep rejection causes separate from positive and unresolved filter notes."""
    if not archived:
        return ()
    if (
        job.canonical_job_id is not None
        and job.duplicate_review_status == JobDuplicateStatus.CONFIRMED
    ):
        reference = canonical_job(session, job.id)
        return archive_reason_labels(
            [
                f"duplicate_reference:{reference.title} — {reference.source_company} (n° {reference.id})"
                if reference is not None else f"duplicate_of:{job.canonical_job_id}"
            ],
            archived=True,
        )

    components = job.score.components if job.score and job.score.components else {}
    application = job.application
    inheritance = components.get("archive_inheritance")
    if (
        application is not None
        and application.status == JobStatus.ARCHIVED
        and isinstance(inheritance, Mapping)
        and type(inheritance.get("schema_version")) is int
        and inheritance.get("schema_version") == 1
        and type(inheritance.get("application_id")) is int
        and inheritance.get("application_id") == application.id
        and type(inheritance.get("source_job_id")) is int
        and inheritance["source_job_id"] > 0
        and all(isinstance(inheritance.get(key), str) for key in ("source_title", "source_company"))
    ):
        return inherited_archive_reason_labels(inheritance)

    reasons = archive_rejection_reasons(components)
    origin = archive_origin_from_components(components)
    # Legacy duplicate merges moved the dossier but omitted its archive
    # provenance. A unique confirmed alias with the original submission URL
    # and the same archive event provides evidence without rewriting runtime data.
    if (
        not reasons
        and origin is None
        and job.canonical_job_id is None
        and application is not None
        and application.status == JobStatus.ARCHIVED
        and application.form_submission_url
    ):
        source = legacy_archive_source(session, job)
        if source is not None:
            source_components = source.score.components if source.score else {}
            source_reasons = archive_rejection_reasons(source_components)
            source_origin = archive_origin_from_components(source_components)
            return inherited_archive_reason_labels({
                "source_job_id": source.id,
                "source_title": source.title,
                "source_company": source.source_company,
                "application_id": application.id,
                "rejection_stage": source_components.get("rejection_stage"),
                "rejection_reasons": source_reasons,
                "origin": source_origin,
            })
    return archive_reason_labels(
        reasons,
        stage=str(components.get("rejection_stage") or ""),
        archived=True,
        origin=origin,
    )
