"""Phase 3 — generate a CV-and-letter application dossier."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from smartapply.concurrency import application_publication, job_operation
from smartapply.config import get_settings
from smartapply.cv import CvAdapter, CvValidator
from smartapply.cv.motivation_validator import MotivationLetterValidator
from smartapply.cv.role_family import classify
from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Application, Job, JobStatus
from smartapply.language import resolve_document_language
from smartapply.llm import LLMProvider
from smartapply.pipeline.application_renderer import ApplicationDocumentRenderer
from smartapply.pipeline.apply.cv_writer import CvWriterMixin, _document_company_label
from smartapply.pipeline.apply.persistence import ApplicationPersistenceMixin
from smartapply.pipeline.output_paths import AtomicApplicationOutput
from smartapply.pipeline.reports import ApplyReport
from smartapply.pipeline.snapshots import PublicationSnapshot, check_cancelled
from smartapply.profile import Profile


class Applier(ApplicationPersistenceMixin, CvWriterMixin):
    """Compose adaptation, validation, rendering and persistence."""

    def __init__(
        self,
        *,
        profile: Profile,
        llm: LLMProvider,
        adapter: CvAdapter,
        validator: CvValidator,
        renderer: ApplicationDocumentRenderer,
        stop_requested: Callable[[], bool] | None = None,
    ):
        self.stop_requested = stop_requested
        self.profile = profile
        self.llm = llm
        self.adapter = adapter
        self.validator = validator
        self.renderer = renderer
        self.letter_validator = MotivationLetterValidator(profile)
        self.settings = get_settings()

    # ============================================================
    # Public API
    # ============================================================

    def apply(
        self,
        job_id: int,
        *,
        form_url: str | None = None,
        force_regenerate: bool = False,
        artifact_dir: Path | None = None,
    ) -> ApplyReport:
        """Generate the CV and motivation letter for one analyzed offer."""
        check_cancelled(self.stop_requested)
        with job_operation(
            [job_id], operation="régénération" if force_regenerate else "génération"
        ):
            report = ApplyReport(job_id=job_id, application_id=None)
            created_reservation = self._reserve_application_id(
                report,
                force_regenerate=force_regenerate,
            )
            owns_publication = False
            try:
                with application_publication(report.job_id, report.application_id):
                    owns_publication = True
                    check_cancelled(self.stop_requested)
                    return self._do_apply(
                        report=report,
                        form_url=form_url,
                        refresh_cache=force_regenerate,
                        artifact_dir=artifact_dir,
                    )
            except Exception:
                if created_reservation and owns_publication:
                    self._release_application_reservation(report)
                raise

    # ============================================================
    # Unified apply path
    # ============================================================

    def _do_apply(
        self,
        *,
        report: ApplyReport,
        form_url: str | None = None,
        refresh_cache: bool = False,
        artifact_dir: Path | None = None,
    ) -> ApplyReport:
        job_id = report.job_id
        job, analysis = self._load_job_analysis(job_id)
        with session_scope() as session:
            application = session.get(Application, report.application_id)
            snapshot = PublicationSnapshot.capture(job, application)
            snapshot.validate(session.get(Job, job_id), application)
        check_cancelled(self.stop_requested)
        offer_language, language_observation = resolve_document_language(
            analysis.offer_language,
            f"{job.title}\n{job.cleaned_description or job.description or ''}",
        )
        if language_observation:
            report.validation_warnings.append(language_observation)
        document_company = _document_company_label(
            job.company,
            analysis.extracted_company_name,
            offer_language,
        )

        adapted, letter = self._generate_draft(
            job=job,
            analysis=analysis,
            offer_language=offer_language,
            document_company=document_company,
            report=report,
            refresh_cache=refresh_cache,
        )
        check_cancelled(self.stop_requested)
        self._validate_letter(letter, adapted, analysis, report, language=offer_language)
        self._validate_cv_offer_alignment(adapted, analysis, job.title, report)
        resolved_form_url = form_url or job.application_url
        report.form_url = resolved_form_url
        artifacts = AtomicApplicationOutput(
            self.settings.output_dir,
            report.application_id,
            final_dir=artifact_dir,
        )
        try:
            self.renderer.render_all(
                report=report,
                adapted=adapted,
                letter=letter,
                job_title=job.title,
                job_company=document_company,
                language=offer_language,
                role_family=classify(analysis, title=job.title),
                analysis=analysis,
                output_dir=artifacts.staging_dir,
            )

            check_cancelled(self.stop_requested)
            # Availability and validation are independent: keep every diagnostic
            # and review decision, but never withhold a rendered dossier because
            # its text needs checking. Invalid edits remain rejected by the reviewer.
            status = JobStatus.READY_FOR_FORM_SUBMISSION
            report.status = status
            with write_session_scope() as session:
                snapshot.validate(
                    session.get(Job, job_id), session.get(Application, report.application_id)
                )
                # From this point cancellation waits for the atomic section.
                artifacts.publish(report)
                self._persist_application(
                    report=report,
                    adapted=adapted,
                    letter=letter,
                    form_url=resolved_form_url,
                    status=status,
                    session=session,
                )

        except Exception:
            artifacts.rollback()
            raise
        artifacts.commit()
        return report
