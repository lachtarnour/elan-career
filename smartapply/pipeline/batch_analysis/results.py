"""Share synchronous analysis contracts, exact cache and atomic result import."""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.orm import Session

from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.models import Job
from smartapply.database.repository.llm_cache import cache_get, cache_set, record_usage
from smartapply.llm import JobFitAssessment, OfferAnalysis
from smartapply.llm.batch import BatchRequest, BatchResult, build_batch_request
from smartapply.llm.prompts import job_analysis, job_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.usage import estimate_cost_usd
from smartapply.offers import build_analyzer_input
from smartapply.offers.analyzer_input import AnalyzerInput
from smartapply.pipeline.process.analysis import (
    _assemble_analysis,
    _ground_offer_excerpts,
    persist_job_analysis,
)
from smartapply.pipeline.process.analysis_admission import is_current_analysis_candidate
from smartapply.pipeline.process.analysis_checkpoint import (
    extraction_key,
    load_extraction,
    save_extraction,
)
from smartapply.pipeline.snapshots import job_snapshot_key
from smartapply.profile import Profile, get_profile

PHASE_SCHEMA = {"extraction": OfferAnalysis, "evaluation": JobFitAssessment}
PHASE_PURPOSE = {"extraction": "job_offer_analysis", "evaluation": "job_fit_assessment"}


def new_item(run_id: str, job: Job, *, session=None, model="") -> AnalysisBatchItem:
    analyzer_input = build_analyzer_input(job)
    saved = (
        load_extraction(session, job, extraction_key(analyzer_input, model)) if session else None
    )
    return AnalysisBatchItem(
        run_id=run_id,
        job_id=job.id,
        job_snapshot=job_snapshot_key(job),
        title=job.title,
        company=job.company,
        analyzer_input=asdict(analyzer_input),
        state="evaluation_pending" if saved else "extraction_pending",
        offer_data=saved.model_dump(mode="json") if saved else None,
        error="",
    )


def current_job(session: Session, item: AnalysisBatchItem, run: AnalysisBatchRun) -> Job | None:
    job = session.get(Job, item.job_id)
    if (
        job is None
        or not is_current_analysis_candidate(session, job)
        or job_snapshot_key(job) != item.job_snapshot
    ):
        item.state = "skipped"
        item.error = "Offre modifiée, déjà analysée, rejetée ou devenue indisponible."
        return None
    if get_profile().model_dump(mode="json") != run.profile_data:
        item.state = "skipped"
        item.error = "Le profil a changé depuis le lancement. Relancez avec le profil actuel."
        return None
    return job


def item_request(
    item: AnalysisBatchItem, run: AnalysisBatchRun, phase: str, max_completion_tokens: int
) -> BatchRequest:
    schema = PHASE_SCHEMA[phase]
    if phase == "extraction":
        system = job_analysis.build_system_prompt()
        user = job_analysis.build_user_prompt_from_input(
            analyzer_input=AnalyzerInput(**item.analyzer_input)
        )
    else:
        system = job_fit.build_system_prompt()
        user = job_fit.build_user_prompt(
            profile=Profile.model_validate(run.profile_data),
            offer_analysis=OfferAnalysis.model_validate(item.offer_data),
        )
    return build_batch_request(
        custom_id=f"{phase}-{item.id}",
        system=system,
        user=user,
        schema=schema,
        model=run.model,
        purpose=PHASE_PURPOSE[phase],
        max_completion_tokens=max_completion_tokens,
    )


def accept_result(
    session: Session, item: AnalysisBatchItem, run: AnalysisBatchRun, phase: str, validated
) -> None:
    job = current_job(session, item, run)
    if job is None:
        return
    item.error = ""
    if phase == "extraction":
        grounded = _ground_offer_excerpts(validated, item.analyzer_input["offer_body"])
        save_extraction(
            session, job, extraction_key(AnalyzerInput(**item.analyzer_input), run.model), grounded
        )
        item.offer_data = grounded.model_dump(mode="json")
        item.state = "evaluation_pending"
    else:
        analysis = _assemble_analysis(OfferAnalysis.model_validate(item.offer_data), validated)
        persist_job_analysis(session, job, analysis, run.model)
        item.state = "completed"


def accept_cache(
    session: Session,
    item: AnalysisBatchItem,
    run: AnalysisBatchRun,
    phase: str,
    request: BatchRequest,
) -> bool:
    cached = cache_get(session, request.cache_key)
    if cached is None:
        return False
    try:
        validated = PHASE_SCHEMA[phase].model_validate_json(cached.response)
    except ValueError:
        session.delete(cached)
        return False
    accept_result(session, item, run, phase, validated)
    record_usage(
        session,
        purpose=PHASE_PURPOSE[phase],
        model=run.model,
        prompt_tokens=cached.prompt_tokens,
        completion_tokens=cached.completion_tokens,
        cost_usd=0,
        cached=True,
        job_id=item.job_id,
        cache_key=request.cache_key,
    )
    return True


def import_result(
    session: Session, run: AnalysisBatchRun, phase: str, request: dict, result: BatchResult
) -> None:
    """Called once per custom ID in the same transaction as journal advancement."""
    item_ids = request["item_ids"]
    items = [session.get(AnalysisBatchItem, item_id) for item_id in item_ids]
    items = [item for item in items if item is not None]
    if not items:
        return
    # Provider usage is counted even for refusals/truncated/invalid JSON, once.
    cost = (
        estimate_cost_usd(
            run.model,
            result.prompt_tokens,
            result.completion_tokens,
            cached_prompt_tokens=result.cached_prompt_tokens,
            cache_write_prompt_tokens=result.cache_write_prompt_tokens,
        )
        * 0.5
    )
    record_usage(
        session,
        purpose=PHASE_PURPOSE[phase],
        model=run.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cached_prompt_tokens=result.cached_prompt_tokens,
        cache_write_prompt_tokens=result.cache_write_prompt_tokens,
        cost_usd=cost,
        job_id=items[0].job_id,
        cache_key=request["cache_key"],
    )
    try:
        validated = result.validate(PHASE_SCHEMA[phase])
    except (ValueError, LLMValidationError) as exc:
        for item in items:
            item.state = f"{phase}_failed"
            item.error = str(exc)
        return
    cache_set(
        session,
        cache_key=request["cache_key"],
        model=run.model,
        response=result.content,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        purpose=PHASE_PURPOSE[phase],
    )
    for item in items:
        accept_result(session, item, run, phase, validated)
