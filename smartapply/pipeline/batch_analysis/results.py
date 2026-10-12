"""Share synchronous analysis contracts, exact cache and atomic result import."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.models import Job
from smartapply.database.repository.llm_cache import cache_get, cache_set, record_usage
from smartapply.llm import (
    JobFitAssessment,
    JobFitDecisionEvaluation,
    OfferAnalysis,
    OfferExtractionOutput,
)
from smartapply.llm.batch import BatchRequest, BatchResult, build_batch_request
from smartapply.llm.fit_context import (
    build_fit_user,
    context_is_current,
    fit_schema,
)
from smartapply.llm.fit_request import check_request_budget
from smartapply.llm.fit_response import FitDecisionResponse, read_fit_response
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm.prompts import job_analysis
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.schemas import JobScopeScreening
from smartapply.llm.usage import estimate_cost_usd
from smartapply.llm_policy import supported_efforts, validate_request_effort
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
from smartapply.pipeline.process.extraction_screening import (
    apply_mission_screen,
    extraction_offer_facts,
)
from smartapply.pipeline.process.scope_screening import (
    load_scope,
    save_scope,
    scope_request,
    scope_result,
)
from smartapply.pipeline.snapshots import job_snapshot_key
from smartapply.profile import get_profile

PHASE_SCHEMA = {
    "screening": JobScopeScreening,
    "extraction": OfferExtractionOutput,
    "evaluation": JobFitAssessment,
}
PHASE_PURPOSE = {
    "screening": "job_scope_screening",
    "extraction": "job_offer_extraction",
    "evaluation": "job_fit_assessment",
}


class FrozenConfigurationError(ValueError):
    """A persisted Batch request lacks its own runnable route."""


def new_item(
    run_id: str,
    job: Job,
    *,
    session=None,
    model="",
    provider="openai",
    effort=None,
    max_completion_tokens=None,
    screening_context,
) -> AnalysisBatchItem:
    analyzer_input = build_analyzer_input(job)
    if session is not None and max_completion_tokens is None:
        raise ValueError("Extraction output budget is required to resume a saved response")
    saved = (
        load_extraction(
            session,
            job,
            extraction_key(
                analyzer_input,
                model,
                max_completion_tokens=max_completion_tokens,
                provider=provider,
                effort=effort,
            ),
        )
        if session
        else None
    )
    screened = None
    screened_out = False
    if session is not None:
        request = scope_request(screening_context, analyzer_input)
        screened = load_scope(session, job.id, request.cache_key)
        if screened is not None:
            screened_out = apply_mission_screen(
                session, job, screened.get("mission_screen"), screening_key=request.cache_key
            )
    return AnalysisBatchItem(
        run_id=run_id,
        job_id=job.id,
        job_snapshot=job_snapshot_key(job),
        title=job.title,
        company=job.source_company,
        analyzer_input=asdict(analyzer_input),
        state=(
            "filtered_after_screening"
            if screened_out
            else "screening_pending"
            if screened is None
            else "evaluation_pending"
            if saved
            else "extraction_pending"
        ),
        scope_data=screened,
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
    if run.fit_context is None:
        raise FrozenConfigurationError("Contexte d'évaluation manquant dans le lot.")
    if not context_is_current(run.fit_context, get_profile()):
        item.state = "skipped"
        item.error = "Le profil ou le contrat d’évaluation a changé depuis le lancement. Relancez avec les réglages actuels."
        return None
    return job


def phase_route(run: AnalysisBatchRun, phase: str) -> tuple[str, str]:
    """Use the journal's original transport/model for each independent phase."""
    if phase == "screening":
        context = run.scope_context or {}
        provider, model, effort = (
            context.get("provider"),
            context.get("model"),
            context.get("effort"),
        )
    elif phase == "evaluation":
        context = run.fit_context or {}
        provider, model, effort = (
            context.get("provider"),
            context.get("model"),
            context.get("effort"),
        )
    else:
        provider, model, effort = run.provider, run.model, run.extraction_effort
    label = {
        "screening": "Préfiltre des missions",
        "extraction": "Extraction des faits de l’offre",
        "evaluation": "Évaluation de l’adéquation",
    }[phase]
    if provider not in {"openai", "anthropic"}:
        raise FrozenConfigurationError(
            f"Configuration IA figée — {label} : fournisseur manquant ou invalide dans ce lot."
        )
    if not model:
        raise FrozenConfigurationError(
            f"Configuration IA figée — {label} : modèle manquant dans ce lot."
        )
    if (provider == "openai" and model.startswith("claude-")) or (
        provider == "anthropic" and model.startswith(("gpt-", "o1", "o3", "o4"))
    ):
        raise FrozenConfigurationError(
            f"Configuration IA figée — {label} : modèle incompatible avec {provider}."
        )
    supported = supported_efforts(provider, model)
    if supported and not effort:
        raise FrozenConfigurationError(
            f"Configuration IA figée — {label} : niveau de raisonnement manquant dans ce lot."
        )
    try:
        validate_request_effort(provider, model, effort)
    except ValueError as exc:
        raise FrozenConfigurationError(
            f"Configuration IA figée — {label} : niveau de raisonnement invalide ({exc})."
        ) from exc
    return provider, model


def item_request(
    item: AnalysisBatchItem, run: AnalysisBatchRun, phase: str, max_completion_tokens: int
) -> BatchRequest:
    provider, model = phase_route(run, phase)
    if phase == "screening":
        return scope_request(
            run.scope_context,
            AnalyzerInput(**item.analyzer_input),
            custom_id=f"screening-{item.id}",
        )
    schema = PHASE_SCHEMA[phase]
    if phase == "extraction":
        effort = run.extraction_effort
        system = job_analysis.build_system_prompt()
        user = job_analysis.build_user_prompt_from_input(
            analyzer_input=AnalyzerInput(**item.analyzer_input)
        )
    else:
        if run.fit_context is None:
            raise LLMValidationError("Contexte d'évaluation manquant dans le lot.")
        system = run.fit_context["system_prompt"]
        schema = fit_schema(run.fit_context)
        user = build_fit_user(
            run.fit_context,
            extraction_offer_facts(item.offer_data),
            extraction_provenance={"analyzer_input": item.analyzer_input},
        )
        check_request_budget(run.fit_context, user)
        max_completion_tokens = run.fit_context["max_completion_tokens"]
        effort = run.fit_context.get("effort")
    return build_batch_request(
        custom_id=f"{phase}-{item.id}",
        system=system,
        user=user,
        schema=schema,
        model=model,
        purpose=PHASE_PURPOSE[phase],
        max_completion_tokens=max_completion_tokens,
        provider=provider,
        effort=effort,
    )


def accept_result(
    session: Session,
    item: AnalysisBatchItem,
    run: AnalysisBatchRun,
    phase: str,
    validated,
    *,
    request_body: dict | None = None,
    request_cache_key: str | None = None,
) -> None:
    job = current_job(session, item, run)
    if job is None:
        return
    item.error = ""
    if phase == "screening":
        screened = scope_result(validated, context=run.scope_context)
        item.scope_data = screened
        key = (
            request_cache_key
            or scope_request(run.scope_context, AnalyzerInput(**item.analyzer_input)).cache_key
        )
        save_scope(session, job.id, key, screened)
        rejected = apply_mission_screen(
            session, job, screened.get("mission_screen"), screening_key=key
        )
        item.state = (
            "filtered_after_screening"
            if rejected
            else "evaluation_pending"
            if item.offer_data
            else "extraction_pending"
        )
    elif phase == "extraction":
        grounded = _ground_offer_excerpts(validated, item.analyzer_input["offer_body"])
        save_extraction(
            session,
            job,
            request_cache_key
            or extraction_key(
                AnalyzerInput(**item.analyzer_input),
                run.model,
                max_completion_tokens=run.max_completion_tokens,
                provider=run.provider,
                effort=run.extraction_effort,
            ),
            grounded,
        )
        item.offer_data = grounded.model_dump(mode="json")
        item.state = "evaluation_pending"
    else:
        offer = extraction_offer_facts(item.offer_data)
        if not isinstance(validated, JobFitAssessment):
            raise LLMValidationError("Réponse d'évaluation incompatible avec le contrat fit.")
        if run.fit_context is None:
            raise LLMValidationError("Diagnostic sans contexte figé")
        if request_body is not None and request_body["messages"][1]["content"] != build_fit_user(
            run.fit_context,
            offer,
            extraction_provenance={"analyzer_input": item.analyzer_input},
        ):
            raise LLMValidationError("Réponse associée à une autre extraction d’offre.")
        validated, diagnostics = evaluate_fit(
            validated,
            run.fit_context,
            offer,
            extraction_provenance={
                "analyzer_input": item.analyzer_input,
                "run_id": run.id,
                **({"effort": run.extraction_effort} if run.extraction_effort else {}),
            },
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
    provider, model = phase_route(run, phase)
    cached = cache_get(session, request.cache_key)
    if cached is None:
        return False
    try:
        validated = validate_request_response(request.body, cached.response)
    except (ValueError, LLMValidationError):
        # A corrupt cached verdict is not a completed screening attempt.
        session.delete(cached)
        return False
    # Publication/context failures are not invalid cached content. Keep the entry
    # and propagate the error, rather than scheduling another extraction.
    accept_result(
        session,
        item,
        run,
        phase,
        validated,
        request_body=request.body,
        request_cache_key=request.cache_key,
    )
    record_usage(
        session,
        purpose=PHASE_PURPOSE[phase],
        model=model,
        requested_model=model,
        reasoning_effort=request.body.get(
            "reasoning_effort", request.body.get("output_config", {}).get("effort")
        ),
        prompt_tokens=cached.prompt_tokens,
        completion_tokens=cached.completion_tokens,
        cost_usd=0,
        cached=True,
        job_id=item.job_id,
        cache_key=request.cache_key,
        provider=provider,
    )
    return True


def import_result(
    session: Session, run: AnalysisBatchRun, phase: str, request: dict, result: BatchResult
) -> None:
    """Called once per custom ID in the same transaction as journal advancement."""
    provider, model = phase_route(run, phase)
    item_ids = request["item_ids"]
    items = [session.get(AnalysisBatchItem, item_id) for item_id in item_ids]
    items = [item for item in items if item is not None]
    if not items:
        return
    # Provider usage is counted even for refusals/truncated/invalid JSON, once.
    served_model = result.model or model
    cost = (
        estimate_cost_usd(
            served_model,
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
        model=served_model,
        requested_model=model,
        reasoning_effort=request["body"].get(
            "reasoning_effort", request["body"].get("output_config", {}).get("effort")
        ),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cached_prompt_tokens=result.cached_prompt_tokens,
        cache_write_prompt_tokens=result.cache_write_prompt_tokens,
        cost_usd=cost,
        job_id=items[0].job_id,
        cache_key=request["cache_key"],
        provider=provider,
    )
    try:
        schema = schema_for_request(request["body"], result.content)
        transport_validated = result.validate(schema)
        validated = validate_request_response(request["body"], result.content)
        if isinstance(validated, FitDecisionResponse):
            validated = replace(
                validated, observations=validated.observations + transport_validated.observations
            )
        # Roll back all publications if one grouped item fails validation. Usage remains recorded.
        with session.begin_nested():
            for item in items:
                accept_result(
                    session,
                    item,
                    run,
                    phase,
                    validated,
                    request_body=request["body"],
                    request_cache_key=request["cache_key"],
                )
    except (ValueError, LLMValidationError) as exc:
        for item in items:
            if phase == "screening":
                fail_open_screening(session, item, run, exc, key=request["cache_key"])
            else:
                item.state = f"{phase}_failed"
                item.error = str(exc)
        return
    cache_set(
        session,
        cache_key=request["cache_key"],
        model=model,
        response=result.content,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        purpose=PHASE_PURPOSE[phase],
        provider=provider,
    )


def fail_open_screening(session, item, run, error, *, key=None):
    if item is None:
        return
    if current_job(session, item, run) is None:
        return
    data = scope_result(error=error, context=run.scope_context)
    item.scope_data = data
    item.state = "evaluation_pending" if item.offer_data else "extraction_pending"
    item.error = ""
    key = key or scope_request(run.scope_context, AnalyzerInput(**item.analyzer_input)).cache_key
    save_scope(session, item.job_id, key, data)


def schema_for_request(body: dict, content: str | None = None):
    stored = body.get("response_format", {}).get("json_schema", {})
    classes = {
        model.__name__: model
        for model in (
            OfferExtractionOutput,
            JobFitAssessment,
            JobFitDecisionEvaluation,
            JobScopeScreening,
        )
    }
    if stored.get("name") not in classes or not stored.get("schema"):
        if content is not None:
            try:
                read_fit_response(content)
            except LLMValidationError:
                pass
            else:
                return JobFitDecisionEvaluation
        raise LLMValidationError("Schéma de réponse absent ou inconnu dans le lot")
    return classes[stored["name"]]


def validate_request_response(body: dict, content: str):
    schema = schema_for_request(body, content)
    saved = body.get("response_format", {}).get("json_schema", {}).get("schema")
    if schema is JobFitDecisionEvaluation:
        parsed = read_fit_response(content, schema=schema)
        observations = list(parsed.observations)
        if not saved:
            observations.append("Stored schema unavailable; technical decisions retained")
        try:
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
        if not issubclass(schema, OfferAnalysis):
            Draft202012Validator(saved).validate(data)
        return schema.model_validate(data)
    except Exception as exc:
        raise LLMValidationError(f"Réponse incompatible avec le schéma enregistré : {exc}") from exc
