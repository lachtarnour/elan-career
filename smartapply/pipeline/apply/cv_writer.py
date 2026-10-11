"""CV and motivation-letter generation/validation helpers."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from smartapply.company_identity import document_company_name
from smartapply.config import get_llm_settings
from smartapply.cv.letter_writer import OBJECTIVE_TEXT_WARNING_PREFIXES, ApplicationTextWriter
from smartapply.cv.motivation_validator import normalize_french_elisions
from smartapply.cv.role_family import (
    classify,
    classify_title,
    cv_title_family_is_compatible,
)
from smartapply.cv.text_hygiene import EVIDENCE_METADATA_PREFIX, evidence_metadata_warnings
from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.language import detect_offer_language
from smartapply.llm import (
    AdaptedCV,
    JobAnalysis,
    LLMError,
    MotivationLetter,
    MotivationLetterRepair,
)
from smartapply.llm.prompts import motivation_letter_repair
from smartapply.llm.routes import provider_for
from smartapply.llm_policy import resolve_route
from smartapply.logging_setup import get_logger
from smartapply.pipeline.reports import ApplyReport

logger = get_logger(__name__)

_ANCHOR_STOPWORDS = {
    "and",
    "avec",
    "chez",
    "data",
    "donnée",
    "données",
    "donnee",
    "donnees",
    "engineer",
    "engineering",
    "for",
    "from",
    "h/f",
    "ingénieur",
    "ingenieur",
    "junior",
    "mid",
    "pour",
    "role",
    "scientist",
    "the",
    "with",
}

_ROLE_PHRASES = (
    "ai engineer",
    "analytics engineer",
    "business analyst",
    "computer vision",
    "data analyst",
    "data engineer",
    "data scientist",
    "deep learning",
    "devops",
    "full stack",
    "fullstack",
    "generative ai",
    "ia engineer",
    "ingénieur data",
    "machine learning",
    "ml engineer",
    "mlops",
    "nlp",
    "product analyst",
    "product data analyst",
    "rag",
    "software engineer",
    "speech",
    "time series",
)

def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().replace("’", "'").split())


def _document_company_label(
    _job_company: str | None,
    extracted_company_name: str | None,
    language: str,
) -> str:
    """Keep the legacy call shape while sharing the document-only decision."""
    return document_company_name(extracted_company_name)


def _word_anchors(text: str) -> set[str]:
    words = {
        word
        for word in re.findall(r"[a-zA-ZÀ-ÿ0-9+#.]{3,}", _norm(text))
        if word not in _ANCHOR_STOPWORDS
    }
    return words


def _offer_anchors(analysis: JobAnalysis, job_title: str) -> set[str]:
    role_text = " ".join(
        [
            job_title or "",
            analysis.role_type or "",
            " ".join(analysis.main_tasks[:4]),
            " ".join(analysis.required_skills),
            " ".join(analysis.cv_keywords_to_include),
        ]
    )
    haystack = _norm(role_text)
    anchors = _word_anchors(role_text)
    for phrase in _ROLE_PHRASES:
        if phrase in haystack:
            anchors.add(phrase)
    return {anchor for anchor in anchors if len(anchor) >= 3}


class CvWriterMixin:
    """Generate and validate CV/motivation-letter content."""

    def _generate_draft(
        self,
        *,
        job: Job,
        analysis: JobAnalysis,
        offer_language: str,
        document_company: str,
        report: ApplyReport,
        refresh_cache: bool = False,
    ) -> tuple[Any, MotivationLetter]:
        """Assemble CV content, write summary and letter together, then review."""
        assembled = self.adapter.adapt(
            analysis,
            job_title=job.title,
            job_company=document_company,
            job_description=job.description or job.cleaned_description or "",
            job_id=job.id,
            refresh_cache=refresh_cache,
        )
        assembled = self._validate_with_auto_fix(assembled, report)
        if not self.validator.validate(assembled).ok:
            raise LLMError("CV structure could not be finalized before text generation")
        context = dict(
            job_title=job.title,
            job_company=document_company,
            job_description=job.description or job.cleaned_description or "",
            language=offer_language,
            job_id=job.id,
            refresh_cache=refresh_cache,
        )
        writer = ApplicationTextWriter(self.profile, self.llm)
        adapted, letter = writer.generate_texts(assembled, analysis, **context)
        return self._review_generated_text(
            adapted,
            letter,
            analysis,
            report=report,
            reference_date=writer.reference_date,
            **context,
        )

    def _review_generated_text(
        self,
        adapted: AdaptedCV,
        letter: MotivationLetter,
        analysis: JobAnalysis,
        *,
        job_title: str,
        job_company: str,
        job_description: str,
        language: str,
        job_id: int | None,
        report: ApplyReport,
        refresh_cache: bool = False,
        reference_date: date | None = None,
    ) -> tuple[AdaptedCV, MotivationLetter]:
        result = ApplicationTextWriter(
            self.profile, self.llm, reference_date=reference_date
        ).review(
            adapted,
            letter,
            analysis,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
            language=language,
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        report.document_review.extend(result.attempts)
        report.validation_errors.extend(result.errors)
        report.validation_warnings.extend(
            warning
            for warning in dict.fromkeys(result.warnings)
            if warning not in report.validation_warnings
        )
        # Refresh objective and metadata hints for the actual returned text,
        # including on review failure; a repaired limit must not leave stale hints.
        report.validation_warnings[:] = [
            warning
            for warning in report.validation_warnings
            if not warning.startswith((EVIDENCE_METADATA_PREFIX, *OBJECTIVE_TEXT_WARNING_PREFIXES))
        ]
        report.validation_warnings.extend(
            warning
            for warning in result.warnings
            if warning.startswith(OBJECTIVE_TEXT_WARNING_PREFIXES)
            and warning not in report.validation_warnings
        )
        report.validation_warnings.extend(
            evidence_metadata_warnings(
                {
                    "cv_title": result.cv.cv_title,
                    "professional_summary": result.cv.professional_summary,
                    "motivation_letter_subject": result.letter.subject,
                    "motivation_letter_body": result.letter.body,
                }
            )
        )
        return result.cv, result.letter

    def _validate_with_auto_fix(self, adapted, report: ApplyReport):
        result = self.validator.validate(adapted)
        if not result.ok:
            adapted, removed = self.validator.auto_fix(adapted)
            result = self.validator.validate(adapted)
            report.validation_warnings.extend(f"auto_fixed:{r}" for r in removed)
        report.validation_warnings.extend(result.warnings)
        report.validation_errors.extend(result.errors)
        return adapted

    def _validate_letter(
        self,
        letter_draft: MotivationLetter,
        adapted,
        analysis: JobAnalysis,
        report: ApplyReport,
        *,
        language: str | None = None,
    ) -> None:
        validation_analysis = (
            analysis.model_copy(update={"offer_language": language})
            if language is not None and language != analysis.offer_language
            else analysis
        )
        result = self.letter_validator.validate(
            letter_draft,
            cv=adapted,
            analysis=validation_analysis,
        )
        report.validation_warnings.extend(
            warning for warning in result.warnings if warning not in report.validation_warnings
        )
        report.validation_errors.extend(result.errors)

    def _repair_letter_once(
        self,
        letter_draft: MotivationLetter,
        adapted: AdaptedCV,
        analysis: JobAnalysis,
        *,
        job_title: str,
        job_company: str,
        language: str,
        job_id: int,
        refresh_cache: bool = False,
    ) -> MotivationLetter:
        """Repair one letter-only defect with at most one smart-model call.

        The original letter is retained when there is no defect, more than one
        defect, a provider failure, or a repair that introduces any new issue.
        """
        initial = self.letter_validator.validate(
            letter_draft,
            cv=adapted,
            analysis=analysis,
        )
        defects = [*initial.errors, *initial.warnings]
        if len(defects) != 1:
            return letter_draft

        prompt = motivation_letter_repair.build_user_prompt(
            profile=self.profile,
            defect=defects[0],
            language=language,
            job_title=job_title,
            job_company=job_company,
            analysis=analysis,
            adapted_cv=adapted,
            letter=letter_draft,
        )
        try:
            provider = provider_for(self.llm, "motivation_letter_repair")
            route = resolve_route(
                getattr(self.llm, "_settings", None) or get_llm_settings(),
                "motivation_letter_repair",
            )
            repair = provider.complete_json(
                system=motivation_letter_repair.build_system_prompt(),
                user=prompt,
                schema=MotivationLetterRepair,
                model=route.model,
                temperature=0.1,
                purpose="motivation_letter_repair",
                job_id=job_id,
                refresh_cache=refresh_cache,
                effort=route.reasoning,
                max_completion_tokens=route.max_output_tokens,
            )
        except LLMError as exc:
            logger.warning(
                "Motivation-letter repair failed for job %s; keeping original: %s",
                job_id,
                exc,
            )
            return letter_draft

        repaired = letter_draft.model_copy(update={"body": repair.body.strip()})
        repaired = repaired.model_copy(
            update={
                "body": normalize_french_elisions(repaired.body, language=language),
            }
        )
        repaired_result = self.letter_validator.validate(
            repaired,
            cv=adapted,
            analysis=analysis,
        )
        if repaired_result.errors or repaired_result.warnings:
            logger.info(
                "Motivation-letter repair rejected for job %s: %s",
                job_id,
                [*repaired_result.errors, *repaired_result.warnings],
            )
            return letter_draft
        return repaired

    def _validate_cv_offer_alignment(
        self,
        adapted,
        analysis: JobAnalysis,
        job_title: str,
        report: ApplyReport,
    ) -> None:
        anchors = _offer_anchors(analysis, job_title)
        offer_family = classify(analysis, title=job_title)
        title_family = classify_title(adapted.cv_title)
        if not cv_title_family_is_compatible(offer_family, title_family):
            report.validation_warnings.append("cv_title_not_offer_anchored")
        summary = _norm(adapted.professional_summary)
        if anchors and not any(anchor in summary for anchor in anchors):
            report.validation_warnings.append("summary_not_offer_anchored")

    def _load_job_analysis(self, job_id: int) -> tuple[Job, JobAnalysis]:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.analysis is None:
                raise ValueError(f"Job {job_id} not analyzed. Run process_pending first.")
            raw = job.analysis.raw_response or {}
            offer_language = raw.get("offer_language") or detect_offer_language(
                f"{job.title}\n{job.cleaned_description or job.description}"
            )
            analysis = JobAnalysis(
                fit_score=(
                    job.analysis.fit_score if job.analysis.fit_score is not None else 0.5
                ),
                role_type=job.analysis.role_type or "",
                seniority=job.analysis.seniority or "",
                domain=job.analysis.domain or "",
                main_tasks=list(job.analysis.main_tasks or []),
                required_skills=list(job.analysis.required_skills or []),
                nice_to_have=list(job.analysis.nice_to_have or []),
                match_reasons=list(job.analysis.match_reasons or []),
                risks=list(job.analysis.risks or []),
                cv_keywords_to_include=list(job.analysis.cv_keywords_to_include or []),
                offer_language=offer_language,
                extracted_company_name=raw.get("extracted_company_name") or "",
                extracted_location=raw.get("extracted_location") or "",
                company_context=raw.get("company_context") or "",
                offer_interest_points=list(raw.get("offer_interest_points") or []),
                constraints=list(raw.get("constraints") or []),
                supporting_excerpts=list(raw.get("supporting_excerpts") or []),
                offer_risks=list(raw.get("offer_risks") or []),
            )
            if job.score is not None:
                _ = job.score.components
            return job, analysis
