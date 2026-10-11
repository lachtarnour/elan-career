"""Reservation and persistence for generated application documents."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smartapply.database import write_session_scope
from smartapply.database.models import Application, Job, JobDuplicateStatus, JobStatus
from smartapply.database.repository import (
    application_for_duplicate_group,
    canonical_job,
    create_or_get_application,
    pending_duplicate_for_group,
    update_status,
    upsert_document,
)
from smartapply.jobsearch.offer_state import STALE_RESERVATION_AFTER as _STALE_RESERVATION_AFTER
from smartapply.jobsearch.offer_state import reservation_is_stale
from smartapply.llm import MotivationLetter
from smartapply.pipeline.errors import (
    ApplicationAlreadyExistsError,
    DuplicateReviewRequiredError,
    StaleTaskResultError,
)
from smartapply.pipeline.reports import ApplyReport
from smartapply.pipeline.snapshots import job_is_active


class ApplicationPersistenceMixin:
    """Persist one CV-and-letter dossier after successful rendering."""

    def _reserve_application_id(
        self,
        report: ApplyReport,
        *,
        force_regenerate: bool = False,
        resume_application_id: int | None = None,
    ) -> bool:
        try:
            with write_session_scope() as session:
                job = session.get(Job, report.job_id)
                if job is None:
                    raise ValueError(f"Offre introuvable: {report.job_id}")
                if job.duplicate_review_status == JobDuplicateStatus.PENDING:
                    raise DuplicateReviewRequiredError(
                        report.job_id,
                        job.possible_duplicate_of_id,
                    )

                root = canonical_job(session, report.job_id)
                if root is None:
                    raise ValueError(f"Offre introuvable: {report.job_id}")
                if int(root.id) != int(report.job_id):
                    report.job_id = int(root.id)
                pending = pending_duplicate_for_group(session, report.job_id)
                if pending is not None:
                    raise DuplicateReviewRequiredError(
                        pending.id,
                        pending.possible_duplicate_of_id,
                    )

                existing = session.execute(
                    select(Application).where(Application.job_id == report.job_id)
                ).scalar_one_or_none()
                group_application = application_for_duplicate_group(session, report.job_id)
                if group_application is not None and group_application.job_id != report.job_id:
                    raise ApplicationAlreadyExistsError(
                        report.job_id,
                        group_application.id,
                    )
                existing = existing or group_application
                if resume_application_id is not None:
                    if (
                        force_regenerate
                        or existing is None
                        or existing.id != resume_application_id
                        or not job_is_active(root)
                        or existing.status != JobStatus.ANALYZED
                        or existing.form_submitted_at is not None
                        or existing.documents
                        or existing.cv_json
                        or existing.cv_docx_path
                        or existing.cv_pdf_path
                    ):
                        raise StaleTaskResultError()
                    report.application_id = existing.id
                    return False
                if existing is not None:
                    if not force_regenerate:
                        if not reservation_is_stale(existing):
                            raise ApplicationAlreadyExistsError(
                                report.job_id,
                                existing.id,
                            )
                        if not job_is_active(root) or existing.form_submitted_at is not None:
                            raise StaleTaskResultError()
                        existing.updated_at = datetime.now(timezone.utc)
                        report.application_id = existing.id
                        return True
                    if not job_is_active(root) or (existing is not None and (
                        existing.form_submitted_at is not None
                        or existing.status in {
                            JobStatus.SENT, JobStatus.INTERVIEW, JobStatus.ARCHIVED,
                        }
                    )):
                        raise StaleTaskResultError()
                    report.application_id = existing.id
                    return False

                if not job_is_active(root):
                    raise StaleTaskResultError()
                application = Application(job_id=report.job_id)
                session.add(application)
                session.flush()
                report.application_id = application.id
                return True
        except IntegrityError as exc:
            raise ApplicationAlreadyExistsError(report.job_id) from exc

    def _release_application_reservation(self, report: ApplyReport) -> None:
        if report.application_id is None:
            return
        with write_session_scope() as session:
            application = session.get(Application, report.application_id)
            if application is None or application.job_id != report.job_id:
                return
            if (
                application.documents
                or application.cv_json
                or application.cv_docx_path
                or application.cv_pdf_path
                or application.form_submitted_at is not None
                or application.status != JobStatus.ANALYZED
            ):
                return
            if application.notes:
                # Preserve an independently edited note while making this
                # cancelled empty reservation immediately retryable.
                application.updated_at = datetime.now(timezone.utc) - _STALE_RESERVATION_AFTER
                return
            session.delete(application)

    def _persist_application(
        self,
        *,
        report: ApplyReport,
        adapted,
        letter: MotivationLetter,
        form_url: str | None,
        status: str,
        session=None,
    ) -> None:
        with (nullcontext(session) if session is not None else write_session_scope()) as session:
            application = (
                session.get(Application, report.application_id)
                if report.application_id is not None
                else None
            )
            if application is None:
                application = create_or_get_application(session, report.job_id)
                report.application_id = application.id

            application.cv_docx_path = report.docx_path
            application.cv_pdf_path = report.cv_pdf_path
            application.cv_json = adapted.model_dump()
            application.validation_warnings = [
                *report.validation_warnings,
                *(f"validation_error:{error}" for error in report.validation_errors),
            ]
            application.status = status
            if status == JobStatus.READY_FOR_FORM_SUBMISSION:
                application.ready_at = datetime.now(timezone.utc)
            application.form_submission_url = form_url

            self.renderer.add_document_rows(session, application.id, report)
            upsert_document(
                session,
                application.id,
                doc_type="cv_json",
                content=json.dumps(adapted.model_dump(), ensure_ascii=False),
            )
            upsert_document(
                session,
                application.id,
                doc_type="motivation_letter",
                content=letter.body,
                extra={"subject": letter.subject},
            )
            upsert_document(
                session,
                application.id,
                doc_type="document_review",
                content=json.dumps(report.document_review, ensure_ascii=False),
                extra={"scope": "cv_title_professional_summary_and_motivation_letter"},
            )
            update_status(session, report.job_id, status)
            report.application_id = application.id
