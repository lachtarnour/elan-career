"""CV and motivation-letter generation/validation helpers."""

from __future__ import annotations

import re
from typing import Any

from smartapply.cv.letter_writer import ApplicationTextWriter
from smartapply.cv.motivation_validator import normalize_french_elisions
from smartapply.cv.role_family import (
    classify,
    classify_title,
    cv_title_family_is_compatible,
)
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

_ANONYMOUS_COMPANY_MARKERS = (
    "confidentiel",
    "non communiqu",
    "anonyme",
)


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().replace("’", "'").split())


def _is_anonymous_company(name: str | None) -> bool:
    lowered = _norm(name)
    return not lowered or any(marker in lowered for marker in _ANONYMOUS_COMPANY_MARKERS)


def _document_company_label(company: str | None, language: str) -> str:
    if not _is_anonymous_company(company):
        return (company or "").strip()
    return "l'entreprise recruteuse" if language == "fr" else "the hiring company"


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
        """Finalize the CV, then write and independently review the letter."""
        adapted, _selection = self.adapter.adapt(
            analysis,
            job_title=job.title,
            job_company=document_company,
            job_id=job.id,
            refresh_cache=refresh_cache,
        )
        adapted = self._validate_with_auto_fix(adapted, report)
        context = dict(
            job_title=job.title,
            job_company=document_company,
            job_description=job.description or job.cleaned_description or "",
            language=offer_language,
            job_id=job.id,
            refresh_cache=refresh_cache,
        )
        writer = ApplicationTextWriter(self.profile, self.llm)
        letter = writer.generate_letter(adapted, analysis, **context)
        return self._review_generated_text(adapted, letter, analysis, report=report, **context)

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
    ) -> tuple[AdaptedCV, MotivationLetter]:
        result = ApplicationTextWriter(self.profile, self.llm).review(
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
        report.validation_warnings.extend(result.warnings)
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
    ) -> None:
        result = self.letter_validator.validate(
            letter_draft,
            cv=adapted,
            analysis=analysis,
        )
        report.validation_warnings.extend(result.warnings)
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
        """Repair one letter-only defect with at most one cheap-model call.

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
            repair = self.llm.complete_json(
                system=motivation_letter_repair.SYSTEM,
                user=prompt,
                schema=MotivationLetterRepair,
                model=self.llm.cheap_model,
                temperature=0.1,
                purpose="motivation_letter_repair",
                job_id=job_id,
                refresh_cache=refresh_cache,
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
            )
            if job.score is not None:
                _ = job.score.components
            return job, analysis
