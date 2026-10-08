"""LLM extraction and profile evaluation for deterministic survivors."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from smartapply.concurrency import job_operation
from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Job, JobStatus
from smartapply.database.repository import (
    mark_analyzed,
    set_analysis,
    update_status,
)
from smartapply.llm import JobAnalysis, JobFitAssessment, OfferAnalysis
from smartapply.llm.fit_context import (
    build_fit_context,
    context_is_current,
)
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.prompts import job_analysis as analysis_prompts
from smartapply.logging_setup import get_logger
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.errors import StaleTaskResultError, TaskCancelledError
from smartapply.pipeline.process.analysis_admission import (
    AnalysisAdmissionMixin,
    is_current_analysis_candidate,
)
from smartapply.pipeline.process.analysis_checkpoint import (
    clear_extraction,
    extraction_key,
    load_extraction,
    save_extraction,
)
from smartapply.pipeline.process.audit import (
    _should_replace_job_company,
    _should_replace_job_location,
)
from smartapply.pipeline.reports import AnalyzeReport
from smartapply.pipeline.snapshots import check_cancelled, job_snapshot_key
from smartapply.profile import get_profile

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


def persist_job_analysis(
    session, job: Job, analysis: JobAnalysis, model: str, *, fit_diagnostics: dict | None = None
) -> None:
    """Publish a validated analysis consistently for immediate and Batch runs.

    The caller owns the transaction and checks current admission and its source
    snapshot before publishing provider results.
    """
    set_analysis(
        session,
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
        model_used=model,
        fit_diagnostics=fit_diagnostics,
    )
    if _should_replace_job_company(job.company, analysis.extracted_company_name):
        job.company = analysis.extracted_company_name.strip()
    if _should_replace_job_location(job.location, analysis.extracted_location):
        job.location = analysis.extracted_location.strip()
    clear_extraction(session, job.id)
    mark_analyzed(session, job.id)
    update_status(session, job.id, JobStatus.ANALYZED)


class AnalysisMixin(AnalysisAdmissionMixin):
    """Analyze selected jobs with the configured LLM provider."""

    def _analyze_in_parallel(
        self,
        jobs: list[Job],
        *,
        allow_existing: bool = False,
        exclude_applications: bool = True,
    ) -> tuple[int, list[dict[str, object]]]:
        """Run ``_analyze_one`` concurrently over the admitted offers.

        Each ``_analyze_one`` opens its own ``session_scope``, so SQLAlchemy
        sessions stay thread-local. OpenAI's SDK is thread-safe. We cap the
        worker pool via ``settings.llm_max_concurrent`` to respect provider
        rate limits.
        """
        if not jobs:
            return 0, []
        if len(jobs) == 1:
            # A service may already own this offer for analyse -> generation.
            # Keep that nested workflow on the reservation's owning thread.
            job = jobs[0]
            try:
                return int(
                    self._analyze_one(
                        job_id=job.id,
                        allow_existing=allow_existing,
                        exclude_applications=exclude_applications,
                    )
                ), []
            except TaskCancelledError:
                return 0, []
            except Exception as exc:
                logger.error("Analysis failed for job %s: %s", job.id, exc)
                return 0, [
                    {
                        "job_id": int(job.id),
                        "title": job.title,
                        "company": job.company,
                        "message": str(exc),
                    }
                ]
        workers = min(self.settings.llm_max_concurrent, len(jobs))
        analyzed = 0
        errors: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._analyze_one,
                    job_id=job.id,
                    allow_existing=allow_existing,
                    exclude_applications=exclude_applications,
                ): job
                for job in jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    if future.result():
                        analyzed += 1
                except TaskCancelledError:
                    continue
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

    def analyze_jobs(
        self,
        job_ids: list[int],
        *,
        force_reanalyze: bool = False,
        exclude_applications: bool = True,
    ) -> AnalyzeReport:
        """Analyze every selected survivor without numeric ranking prerequisites."""
        unique_ids = list(dict.fromkeys(int(job_id) for job_id in job_ids))
        if not unique_ids:
            return AnalyzeReport(0, 0, 0, 0)

        jobs = self.analysis_candidates(
            unique_ids,
            include_analyzed=True,
            exclude_applications=exclude_applications,
        )
        found_ids = {int(job.id) for job in jobs}
        to_analyze = [
            job
            for job in jobs
            if force_reanalyze or (job.analyzed_at is None and job.analysis is None)
        ]
        already_analyzed = len(jobs) - len(to_analyze)

        analyzed, errors = self._analyze_in_parallel(
            to_analyze,
            allow_existing=force_reanalyze,
            exclude_applications=exclude_applications,
        )
        return AnalyzeReport(
            requested=len(unique_ids),
            already_analyzed=already_analyzed,
            analyzed=analyzed,
            skipped_missing=len(set(unique_ids) - found_ids),
            errors=errors,
            cancelled=bool(getattr(self, "stop_requested", None) and self.stop_requested()),
        )

    # -------------------- internals --------------------

    def _analysis_profile(self):
        """Resolve the same profile source for a request and its stale checks."""
        return get_profile() if getattr(self, "_uses_global_profile", False) else self.profile

    def _analyze_one(
        self,
        *,
        job_id: int,
        allow_existing: bool = True,
        exclude_applications: bool = False,
    ) -> bool:
        stop_requested = getattr(self, "stop_requested", None)
        check_cancelled(stop_requested)
        with job_operation([job_id], operation="analyse"):
            check_cancelled(stop_requested)
            evaluation_profile = self._analysis_profile().model_copy(deep=True)
            with session_scope() as session:
                job = session.get(Job, job_id)
                if job is None or not is_current_analysis_candidate(
                    session,
                    job,
                    include_analyzed=allow_existing,
                    exclude_applications=exclude_applications,
                    exclude_reserved=True,
                ):
                    return False
                snapshot = job_snapshot_key(job)
                analyzer_input = build_analyzer_input(job)
            user_prompt = analysis_prompts.build_user_prompt_from_input(
                analyzer_input=analyzer_input,
            )
            key = extraction_key(analyzer_input, self.llm.cheap_model)
            with write_session_scope() as session:
                offer = load_extraction(session, session.get(Job, job_id), key)
            check_cancelled(stop_requested)
            if offer is None:
                offer = self.llm.complete_json(
                    system=analysis_prompts.build_system_prompt(),
                    user=user_prompt,
                    schema=OfferAnalysis,
                    model=self.llm.cheap_model,
                    purpose="job_offer_analysis",
                    job_id=job_id,
                )
            # A changed offer must not consume a second paid call.
            with write_session_scope() as session:
                current = session.get(Job, job_id)
                if (
                    current is None
                    or job_snapshot_key(current) != snapshot
                    or not is_current_analysis_candidate(
                        session,
                        current,
                        include_analyzed=allow_existing,
                        exclude_applications=exclude_applications,
                        exclude_reserved=True,
                    )
                ):
                    raise StaleTaskResultError()
                offer = _ground_offer_excerpts(offer, analyzer_input.offer_body)
                save_extraction(session, current, key, offer)
            check_cancelled(stop_requested)
            context = build_fit_context(evaluation_profile, self.llm.cheap_model)
            if not context_is_current(context, self._analysis_profile()):
                raise StaleTaskResultError()
            fit, diagnostics = complete_fit(
                self.llm,
                context,
                offer,
                job_id=job_id,
                extraction_provenance={
                    "analyzer_input": asdict(analyzer_input),
                    "extraction_key": key,
                    "model": self.llm.cheap_model,
                },
            )
            check_cancelled(stop_requested)
            analysis = _assemble_analysis(offer, fit)
            with write_session_scope() as session:
                if context["model"] != self.llm.cheap_model or not context_is_current(
                    context, self._analysis_profile()
                ):
                    raise StaleTaskResultError()
                job = session.get(Job, job_id)
                if (
                    job is None
                    or job_snapshot_key(job) != snapshot
                    or not is_current_analysis_candidate(
                        session,
                        job,
                        include_analyzed=allow_existing,
                        exclude_applications=exclude_applications,
                        exclude_reserved=True,
                    )
                ):
                    raise StaleTaskResultError()
                persist_job_analysis(
                    session, job, analysis, self.llm.cheap_model, fit_diagnostics=diagnostics
                )
            return True
