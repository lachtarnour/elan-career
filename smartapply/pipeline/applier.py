"""Phase 3 — generate a CV-and-letter application dossier."""

from __future__ import annotations

from pathlib import Path

from smartapply.config import get_settings
from smartapply.cv import CvAdapter, CvValidator
from smartapply.cv.motivation_validator import MotivationLetterValidator
from smartapply.cv.role_family import classify
from smartapply.database.models import JobStatus
from smartapply.llm import LLMProvider
from smartapply.pipeline.application_renderer import ApplicationDocumentRenderer
from smartapply.pipeline.apply.cv_writer import CvWriterMixin, _document_company_label
from smartapply.pipeline.apply.persistence import ApplicationPersistenceMixin
from smartapply.pipeline.output_paths import AtomicApplicationOutput
from smartapply.pipeline.reports import ApplyReport
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
    ):
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
        report = ApplyReport(job_id=job_id, application_id=None)
        created_reservation = self._reserve_application_id(
            report,
            force_regenerate=force_regenerate,
        )
        try:
            return self._do_apply(
                report=report,
                form_url=form_url,
                refresh_cache=force_regenerate,
                artifact_dir=artifact_dir,
            )
        except Exception:
            if created_reservation:
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
        offer_language = analysis.offer_language or "fr"
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
        self._validate_letter(letter, adapted, analysis, report)
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

            artifacts.publish(report)
            # Availability and validation are independent: keep every diagnostic
            # and review decision, but never withhold a rendered dossier because
            # its text needs checking. Invalid edits remain rejected by the reviewer.
            status = JobStatus.READY_FOR_FORM_SUBMISSION
            report.status = status
            self._persist_application(
                report=report,
                adapted=adapted,
                letter=letter,
                form_url=resolved_form_url,
                status=status,
            )
        except Exception:
            artifacts.rollback()
            raise
        artifacts.commit()
        return report
