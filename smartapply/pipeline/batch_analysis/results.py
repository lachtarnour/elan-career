"""Share synchronous analysis contracts, exact cache and atomic result import."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from smartapply.config import get_settings
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.models import Job
from smartapply.database.repository.llm_cache import cache_get, cache_set, record_usage
from smartapply.llm import (
    JobFitAssessment,
    JobFitDecisionEvaluation,
    JobFitEvaluation,
    OfferAnalysis,
)
from smartapply.llm.batch import BatchRequest, BatchResult, build_batch_request
from smartapply.llm.fit_context import (
    build_fit_user,
    candidate_facts,
    context_is_current,
    fit_schema,
)
from smartapply.llm.fit_request import check_request_budget
from smartapply.llm.fit_response import FitEvaluationResponse, read_fit_response
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm.prompts import job_analysis
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

PHASE_SCHEMA = {"extraction": OfferAnalysis, "evaluation": JobFitDecisionEvaluation}
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
    if (
        run.fit_context is not None
        and (
            run.model != get_settings().openai_model_cheap
            or not context_is_current(run.fit_context, get_profile())
        )
    ) or (
        run.fit_context is None
        and candidate_facts(get_profile())
        != candidate_facts(Profile.model_validate(run.profile_data))
    ):
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
        if run.fit_context is None:
            raise LLMValidationError(
                "Ancien lot sans contexte d'évaluation figé : reprise explicite nécessaire."
            )
        system = run.fit_context["system_prompt"]
        schema = fit_schema(run.fit_context)
        user = build_fit_user(
            run.fit_context,
            OfferAnalysis.model_validate(item.offer_data),
            extraction_provenance={"analyzer_input": item.analyzer_input},
        )
        check_request_budget(run.fit_context, user)
        max_completion_tokens = run.fit_context["max_completion_tokens"]
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
    session: Session,
    item: AnalysisBatchItem,
    run: AnalysisBatchRun,
    phase: str,
    validated,
    *,
    request_body: dict | None = None,
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
        offer = OfferAnalysis.model_validate(item.offer_data)
        diagnostics = None
        if isinstance(
            validated, (JobFitEvaluation, JobFitDecisionEvaluation, FitEvaluationResponse)
        ):
            if run.fit_context is None:
                raise LLMValidationError("Diagnostic sans contexte figé")
            if request_body is not None and request_body["messages"][1][
                "content"
            ] != build_fit_user(
                run.fit_context,
                offer,
                extraction_provenance={"analyzer_input": item.analyzer_input},
            ):
                raise LLMValidationError("Réponse associée à une autre extraction d’offre.")
            validated, diagnostics = evaluate_fit(
                validated,
                run.fit_context,
                offer,
                extraction_provenance={"analyzer_input": item.analyzer_input, "run_id": run.id},
            )
            diagnostics["request_user"] = (
                request_body["messages"][1]["content"]
                if request_body
                else build_fit_user(
                    run.fit_context,
                    offer,
                    extraction_provenance={"analyzer_input": item.analyzer_input},
                )
            )
            diagnostics["request_budget"] = check_request_budget(
                run.fit_context, diagnostics["request_user"]
            )
        elif request_body is not None:
            diagnostics = {
                "policy_version": "legacy_llm_fit",
                "request": request_body,
                "profile_snapshot": run.profile_data,
                "offer_facts": item.offer_data,
            }
        analysis = _assemble_analysis(offer, validated)
        persist_job_analysis(session, job, analysis, run.model, fit_diagnostics=diagnostics)
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
        validated = validate_request_response(request.body, cached.response)
    except (ValueError, LLMValidationError):
        session.delete(cached)
        return False
    # Publication/context failures are not invalid cached content. Keep the entry
    # and propagate the error, rather than scheduling another extraction.
    accept_result(session, item, run, phase, validated, request_body=request.body)
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
        schema = schema_for_request(request["body"], result.content)
        transport_validated = result.validate(schema)
        validated = validate_request_response(request["body"], result.content)
        if isinstance(validated, FitEvaluationResponse):
            validated = replace(
                validated, observations=validated.observations + transport_validated.observations
            )
        # Roll back all publications if one grouped item fails validation. Usage remains recorded.
        with session.begin_nested():
            for item in items:
                accept_result(session, item, run, phase, validated, request_body=request["body"])
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


def schema_for_request(body: dict, content: str | None = None):
    stored = body.get("response_format", {}).get("json_schema", {})
    classes = {
        model.__name__: model
        for model in (OfferAnalysis, JobFitAssessment, JobFitEvaluation, JobFitDecisionEvaluation)
    }
    if stored.get("name") in {JobFitEvaluation.__name__, JobFitDecisionEvaluation.__name__}:
        return classes[stored["name"]]
    if stored.get("name") not in classes or not stored.get("schema"):
        if content is not None:
            try:
                read_fit_response(content)
            except LLMValidationError:
                pass
            else:
                return JobFitEvaluation
        raise LLMValidationError(
            "Schéma historique absent ou inconnu : reprise explicite nécessaire"
        )
    return classes[stored["name"]]


def validate_request_response(body: dict, content: str):
    schema = schema_for_request(body, content)
    saved = body.get("response_format", {}).get("json_schema", {}).get("schema")
    if schema in {JobFitEvaluation, JobFitDecisionEvaluation}:
        parsed = read_fit_response(content, schema=schema)
        observations = list(parsed.observations)
        try:
            if not saved:
                observations.append(
                    "Generation schema unavailable; required scoring inputs identified without ambiguity"
                )
            else:
                observations.extend(
                    f"Stored schema annotation {error.json_path}: {error.message}"
                    for error in Draft202012Validator(copy.deepcopy(saved)).iter_errors(
                        copy.deepcopy(parsed.raw)
                    )
                )
        except Exception as exc:
            observations.append(f"Stored schema inspection: {type(exc).__name__}: {exc}")
        return replace(parsed, observations=observations)
    try:
        data = json.loads(content)
        if schema is not OfferAnalysis:
            Draft202012Validator(saved).validate(data)
        return schema.model_validate(data)
    except Exception as exc:
        raise LLMValidationError(f"Réponse incompatible avec le schéma enregistré : {exc}") from exc
