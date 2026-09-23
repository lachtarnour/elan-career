"""Render the CV and motivation letter for one application.

DOCX is generated for the candidate (editable). HTML and PDF are the
canonical recruiter-facing artifacts. PDF failures keep the editable DOCX
available and are surfaced as validation warnings.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from smartapply.config import get_settings
from smartapply.cv import CvDocxRenderer, HtmlApplicationRenderer
from smartapply.cv.role_contracts import offer_anchored_categories
from smartapply.cv.skill_display import compact_sparse_secondary_categories
from smartapply.database.repository import upsert_document
from smartapply.llm import MotivationLetter
from smartapply.logging_setup import get_logger
from smartapply.pipeline.output_paths import application_document_stems, application_output_dir
from smartapply.profile import Profile

if TYPE_CHECKING:
    from smartapply.pipeline.reports import ApplyReport

logger = get_logger(__name__)


class ApplicationDocumentRenderer:
    """Render every artifact (DOCX, HTML, PDF) for one application."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.docx = CvDocxRenderer(profile)
        self.html = HtmlApplicationRenderer(profile)
        self.settings = get_settings()

    def render_all(
        self,
        *,
        report: ApplyReport,
        adapted,
        letter: MotivationLetter,
        job_title: str,
        job_company: str,
        language: str,
        role_family: str | None = None,
        analysis=None,
        output_dir: Path | None = None,
    ) -> None:
        """Generate every artifact and populate the corresponding report fields."""
        out_dir = output_dir or application_output_dir(
            self.settings.output_dir,
            report.application_id,
        )
        cv_stem, letter_stem = application_document_stems(
            self.profile.template_style.filename_candidate_name or self.profile.identity.full_name,
            job_title,
        )
        display_adapted, skill_merges = compact_sparse_secondary_categories(
            adapted,
            primary_family=role_family or "",
            enabled=getattr(self.settings, "cv_merge_sparse_secondary_skills", True),
            min_standalone_skills=getattr(
                self.settings,
                "cv_secondary_skill_block_min_size",
                4,
            ),
            protected_categories=(
                offer_anchored_categories(adapted, analysis) if analysis else None
            ),
        )
        if skill_merges:
            logger.info(
                "Compacted sparse secondary skill categories for job %s: %s",
                report.job_id,
                [f"{merge.source_category}->{merge.target_category}" for merge in skill_merges],
            )

        # ---- DOCX (always — candidate fallback) ----
        docx_path = out_dir / f"{cv_stem}.docx"
        self.docx.save(display_adapted, docx_path)
        report.docx_path = str(docx_path)

        # ---- HTML sources ----
        cv_html_path = out_dir / f"{cv_stem}.html"
        letter_html_path = out_dir / f"{letter_stem}.html"
        self.html.save_cv_html(display_adapted, cv_html_path)
        self.html.save_letter_html(
            letter=letter,
            job_title=job_title,
            job_company=job_company,
            path=letter_html_path,
            language=language,
            letter_headline=display_adapted.cv_title,
        )
        report.cv_html_path = str(cv_html_path)
        report.letter_html_path = str(letter_html_path)

        # ---- PDF (best effort — DOCX is the fallback) ----
        cv_pdf_path = out_dir / f"{cv_stem}.pdf"
        letter_pdf_path = out_dir / f"{letter_stem}.pdf"
        try:
            self.html.save_cv_pdf(display_adapted, cv_pdf_path)
            self.html.save_letter_pdf(
                letter=letter,
                job_title=job_title,
                job_company=job_company,
                path=letter_pdf_path,
                language=language,
                letter_headline=display_adapted.cv_title,
            )
            report.cv_pdf_path = str(cv_pdf_path)
            report.letter_pdf_path = str(letter_pdf_path)
        except Exception as exc:
            cv_pdf_path.unlink(missing_ok=True)
            letter_pdf_path.unlink(missing_ok=True)
            report.cv_pdf_path = None
            report.letter_pdf_path = None
            logger.warning("HTML PDF rendering skipped: %s", exc)
            report.validation_warnings.append(f"pdf_generation_skipped:{exc}")

    @staticmethod
    def add_document_rows(session, application_id: int, report: ApplyReport) -> None:
        """Persist every generated artifact in ``generated_documents``."""
        if report.docx_path:
            upsert_document(session, application_id, doc_type="cv_docx", path=report.docx_path)
        for doc_type, path in [
            ("cv_html", report.cv_html_path),
            ("cv_pdf", report.cv_pdf_path),
            ("motivation_letter_html", report.letter_html_path),
            ("motivation_letter_pdf", report.letter_pdf_path),
        ]:
            if path:
                upsert_document(session, application_id, doc_type=doc_type, path=path)
