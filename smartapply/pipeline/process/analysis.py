"""LLM analysis phase for shortlisted jobs."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import or_

from smartapply.database import session_scope
from smartapply.database.models import Job, JobDuplicateStatus, JobStatus
from smartapply.database.repository import (
    mark_analyzed,
    set_analysis,
    update_status,
)
from smartapply.llm import JobAnalysis, JobFitAssessment, OfferAnalysis
from smartapply.llm.prompts import job_analysis as analysis_prompts
from smartapply.llm.prompts import job_fit as fit_prompts
from smartapply.logging_setup import get_logger
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.process.audit import (
    _should_replace_job_company,
    _should_replace_job_location,
)
from smartapply.pipeline.reports import AnalyzeReport

logger = get_logger(__name__)


def _ground_offer_excerpts(offer: OfferAnalysis, body: str) -> OfferAnalysis:
    """Discard unverifiable quotes and surface an offer-quality concern."""
    verified = []
    for quote in offer.supporting_excerpts:
        if not quote.strip():
            continue
        match = re.search(r"\s+".join(re.escape(word) for word in quote.split()), body)
        if match:
            # Whitespace may be normalized by the model; store the exact body
            # span so every retained excerpt remains directly verifiable.
            verified.append(match.group(0))
    if verified == offer.supporting_excerpts:
        return offer
    risks = list(offer.offer_risks)
    if len(verified) != len(offer.supporting_excerpts):
        risks.append(
            "Some supporting excerpts could not be verified against the offer body "
            "and were excluded; the extracted requirements need verification."
        )
    return offer.model_copy(
        update={
            "supporting_excerpts": verified,
            "offer_risks": list(dict.fromkeys(risks)),
        }
    )


def _assemble_analysis(offer: OfferAnalysis, fit: JobFitAssessment) -> JobAnalysis:
    """Only extraction owns offer fields; matching contributes three fields."""
    return JobAnalysis(
        **offer.model_dump(),
        fit_score=fit.fit_score,
        match_reasons=fit.match_reasons,
        risks=list(dict.fromkeys([*offer.offer_risks, *fit.risks])),
    )


class AnalysisMixin:
    """Analyze selected jobs with the configured LLM provider."""

    def _analyze_in_parallel(
        self,
        shortlisted_jobs: list[Job],
    ) -> tuple[int, list[dict[str, object]]]:
        """Run ``_analyze_one`` concurrently over the shortlist.

        Each ``_analyze_one`` opens its own ``session_scope``, so SQLAlchemy
        sessions stay thread-local. OpenAI's SDK is thread-safe. We cap the
        worker pool via ``settings.llm_max_concurrent`` to respect provider
        rate limits.
        """
        if not shortlisted_jobs:
            return 0, []
        workers = min(self.settings.llm_max_concurrent, len(shortlisted_jobs))
        analyzed = 0
        errors: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._analyze_one, job_id=job.id): job for job in shortlisted_jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    future.result()
                    analyzed += 1
                except Exception as e:
                    logger.error("Analysis failed for job %s: %s", job.id, e)
                    errors.append(
                        {
                            "job_id": int(job.id),
                            "title": job.title,
                            "company": job.company,
                            "message": str(e),
                        }
                    )
        errors.sort(key=lambda error: int(error["job_id"]))
        return analyzed, errors

    def analyze_jobs(self, job_ids: list[int]) -> AnalyzeReport:
        """Analyze exactly the selected jobs that have not already been analyzed."""
        unique_ids = list(dict.fromkeys(int(job_id) for job_id in job_ids))
        if not unique_ids:
            return AnalyzeReport(0, 0, 0, 0)

        with session_scope() as s:
            jobs = (
                s.query(Job)
                .filter(Job.id.in_(unique_ids))
                .filter(Job.archived_at.is_(None))
                .filter(
                    or_(
                        Job.duplicate_review_status.is_(None),
                        Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                    )
                )
                .all()
            )
            found_ids = {int(job.id) for job in jobs}
            to_analyze = [job for job in jobs if job.analyzed_at is None]
            already_analyzed = len(jobs) - len(to_analyze)

        analyzed, errors = self._analyze_in_parallel(to_analyze)
        return AnalyzeReport(
            requested=len(unique_ids),
            already_analyzed=already_analyzed,
            analyzed=analyzed,
            skipped_missing=len(set(unique_ids) - found_ids),
            errors=errors,
        )

    # -------------------- internals --------------------

    def _analyze_one(self, *, job_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            analyzer_input = build_analyzer_input(job)
            user_prompt = analysis_prompts.build_user_prompt_from_input(
                analyzer_input=analyzer_input,
            )
            offer = self.llm.complete_json(
                system=analysis_prompts.build_system_prompt(),
                user=user_prompt,
                schema=OfferAnalysis,
                model=self.llm.cheap_model,
                purpose="job_offer_analysis",
                job_id=job.id,
            )
            offer = _ground_offer_excerpts(offer, analyzer_input.offer_body)
            fit = self.llm.complete_json(
                system=fit_prompts.build_system_prompt(),
                user=fit_prompts.build_user_prompt(
                    profile=self.profile,
                    offer_analysis=offer,
                ),
                schema=JobFitAssessment,
                model=self.llm.cheap_model,
                purpose="job_fit_assessment",
                job_id=job.id,
            )
            analysis = _assemble_analysis(offer, fit)
            set_analysis(
                s,
                job.id,
                fit_score=analysis.fit_score,
                role_type=analysis.role_type,
                seniority=analysis.seniority,
                domain=analysis.domain,
                main_tasks=analysis.main_tasks,
                required_skills=analysis.required_skills,
                nice_to_have=analysis.nice_to_have,
                match_reasons=analysis.match_reasons,
                risks=analysis.risks,
                cv_keywords_to_include=analysis.cv_keywords_to_include,
                raw_response=analysis.model_dump(),
                model_used=self.llm.cheap_model,
            )
            if _should_replace_job_company(job.company, analysis.extracted_company_name):
                job.company = analysis.extracted_company_name.strip()
            if _should_replace_job_location(job.location, analysis.extracted_location):
                job.location = analysis.extracted_location.strip()
            mark_analyzed(s, job.id)
            update_status(s, job.id, JobStatus.ANALYZED)
