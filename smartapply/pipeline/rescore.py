"""Explicit, frozen fit-only recalculation; preparing a plan never calls a provider."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smartapply.concurrency import job_operation
from smartapply.config import get_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem
from smartapply.database.models import Job, JobStatus
from smartapply.llm.fit_context import build_fit_context, context_is_current, fingerprint
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.prompts import job_analysis
from smartapply.llm.schemas import JobAnalysis, OfferAnalysis
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.errors import StaleTaskResultError
from smartapply.pipeline.process.analysis import _ground_offer_excerpts
from smartapply.pipeline.process.analysis_admission import (
    analysis_group_available,
    reserved_analysis_job_ids,
)
from smartapply.pipeline.snapshots import job_is_active, job_snapshot_key
from smartapply.profile import get_profile

EXCLUDED_STATUSES = {
    JobStatus.SENT,
    JobStatus.INTERVIEW,
    JobStatus.REJECTED,
    JobStatus.QUALITY_REJECTED,
    JobStatus.ARCHIVED,
    "closed",
    "withdrawn",
    "removed",
}
SELECTION_DESCRIPTION = (
    "Active canonical offers with a successful analysis and fit; ready unsent applications included. "
    "Sent/interview/rejected/archived/quality_rejected/closed/withdrawn/removed offers, "
    "secondary duplicates, pending duplicate groups and reserved workflows excluded."
)


def _load(session, ids=None):
    statement = (
        select(Job)
        .options(selectinload(Job.analysis), selectinload(Job.application), selectinload(Job.score))
        .order_by(Job.id)
    )
    if ids is not None:
        statement = statement.where(Job.id.in_(ids))
    return list(session.scalars(statement))


def eligibility_reason(session, job, reserved):
    if job is None:
        return "missing"
    if not job_is_active(job) or job.status in EXCLUDED_STATUSES:
        return "inactive_or_excluded_status"
    if job.canonical_job_id is not None:
        return "secondary_duplicate"
    application = job.application
    if application and (
        application.form_submitted_at is not None or application.status in EXCLUDED_STATUSES
    ):
        return "submitted_or_excluded_application"
    if job.id in reserved or not analysis_group_available(
        session, job.id, exclude_applications=False
    ):
        return "protected_or_reserved_group"
    if job.analysis is None or job.analysis.fit_score is None:
        return "no_successful_fit"
    return None


def _analysis_key(job):
    return fingerprint(
        {
            column.name: getattr(job.analysis, column.name)
            for column in job.analysis.__table__.columns
        }
    )


def _source_matches(saved: dict, current: dict, offer: OfferAnalysis):
    """Only documented extraction corrections may differ from source metadata."""
    for field, value in current.items():
        if saved.get(field) == value:
            continue
        correction = {
            "company": offer.extracted_company_name,
            "location": offer.extracted_location,
        }.get(field)
        if not correction or value != correction:
            return False
    return True


def reusable_offer(session, job):
    raw = job.analysis.raw_response
    # Defaults on historical schemas are not evidence that missing facts existed.
    if not isinstance(raw, dict) or not set(OfferAnalysis.model_fields) <= raw.keys():
        return None, None
    try:
        offer = OfferAnalysis.model_validate(
            {field: raw[field] for field in OfferAnalysis.model_fields}
        )
        JobAnalysis.model_validate(raw)
    except ValidationError:
        return None, None
    current = asdict(build_analyzer_input(job))
    diagnostics = job.analysis.fit_diagnostics or {}
    provenance = diagnostics.get("extraction_provenance", {})
    if provenance.get("analyzer_input") and _source_matches(
        provenance["analyzer_input"], current, offer
    ):
        return offer, provenance
    for item in session.scalars(
        select(AnalysisBatchItem)
        .where(AnalysisBatchItem.job_id == job.id, AnalysisBatchItem.offer_data.is_not(None))
        .order_by(AnalysisBatchItem.id.desc())
    ):
        if item.offer_data == offer.model_dump(mode="json") and _source_matches(
            item.analyzer_input, current, offer
        ):
            return offer, {"analyzer_input": item.analyzer_input, "run_id": item.run_id}
    return None, None


def prepare_rescore_plan(
    *, job_ids=None, all_active=False, include_extractions=False, force=False, profile=None
) -> dict:
    if bool(job_ids) == bool(all_active):
        raise ValueError("Choose explicit job IDs or all-active, exclusively")
    context = build_fit_context(profile or get_profile(), get_settings().openai_model_cheap)
    entries = []
    with session_scope() as session:
        reserved = reserved_analysis_job_ids(session)
        jobs = _load(session, None if all_active else list(dict.fromkeys(job_ids)))
        for job in jobs:
            entry = {"job_id": job.id, "title": job.title, "company": job.company}
            reason = eligibility_reason(session, job, reserved)
            if reason:
                entries.append({**entry, "operation": "excluded", "reason": reason})
                continue
            raw = job.analysis.raw_response
            try:
                if not isinstance(raw, dict):
                    raise ValueError("raw_response is not an object")
                JobAnalysis.model_validate(raw)
            except (ValueError, ValidationError):
                entries.append(
                    {
                        **entry,
                        "operation": "unresolved",
                        "reason": "incompatible_historical_raw_response",
                    }
                )
                continue
            offer, provenance = reusable_offer(session, job)
            diagnostics = job.analysis.fit_diagnostics or {}
            compatible = (
                diagnostics.get("context", {}).get("context_fingerprint")
                == context["context_fingerprint"]
            )
            compatible = (
                compatible
                and offer is not None
                and diagnostics.get("offer_fingerprint")
                == fingerprint(offer.model_dump(mode="json"))
            )
            operation = (
                "already_compatible"
                if compatible and not force
                else (
                    "evaluate_fit"
                    if offer is not None
                    else "reextract_then_fit"
                    if include_extractions
                    else "needs_extraction"
                )
            )
            entries.append(
                {
                    **entry,
                    "operation": operation,
                    "job_snapshot": job_snapshot_key(job),
                    "analysis_fingerprint": _analysis_key(job),
                    "offer_facts": offer.model_dump(mode="json") if offer else None,
                    "extraction_provenance": provenance,
                    "analyzer_input": asdict(build_analyzer_input(job)),
                    "extraction_user": job_analysis.build_user_prompt_from_input(
                        analyzer_input=build_analyzer_input(job)
                    )
                    if operation == "reextract_then_fit"
                    else None,
                }
            )
        found = {job.id for job in jobs}
        for job_id in set(job_ids or []) - found:
            entries.append({"job_id": job_id, "operation": "excluded", "reason": "missing"})
    plan = {
        "plan_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selection": SELECTION_DESCRIPTION,
        "context": context,
        "entries": entries,
        "extraction_request": {
            "system": job_analysis.build_system_prompt(),
            "schema": OfferAnalysis.model_json_schema(),
            "temperature": 0.2,
            "max_completion_tokens": get_settings().openai_max_completion_tokens,
        },
    }
    plan["plan_fingerprint"] = fingerprint(plan)
    return plan


def plan_summary(plan):
    counts = {}
    for entry in plan["entries"]:
        operation = entry["operation"]
        counts[operation] = counts.get(operation, 0) + 1
    return {
        "selection": plan["selection"],
        "counts": counts,
        "evaluation_requests": counts.get("evaluate_fit", 0) + counts.get("reextract_then_fit", 0),
        "extraction_requests": counts.get("reextract_then_fit", 0),
        "unapproved_extractions": counts.get("needs_extraction", 0),
        "plan_fingerprint": plan["plan_fingerprint"],
        "entries": plan["entries"],
    }


def execute_rescore_plan(plan: dict, provider, *, approved=False, profile=None) -> list[dict]:
    if approved is not True:
        raise ValueError("Explicit approval of this frozen plan is required")
    unsigned = {key: value for key, value in plan.items() if key != "plan_fingerprint"}
    if plan.get("plan_version") != 1 or fingerprint(unsigned) != plan.get("plan_fingerprint"):
        raise ValueError("Invalid or modified rescore plan")
    context = plan["context"]
    results = []
    for entry in plan["entries"]:
        job_id = entry["job_id"]
        operation = entry["operation"]
        if operation not in {"evaluate_fit", "reextract_then_fit"}:
            results.append({"job_id": job_id, "status": operation})
            continue
        try:
            with job_operation([job_id], operation="recalcul fit"):

                def validate_source(session, job_id=job_id, entry=entry):
                    if context[
                        "model"
                    ] != get_settings().openai_model_cheap or not context_is_current(
                        context, profile or get_profile()
                    ):
                        raise StaleTaskResultError()
                    job = session.get(Job, job_id)
                    reason = eligibility_reason(session, job, reserved_analysis_job_ids(session))
                    if reason:
                        raise ValueError(f"Offer no longer admissible: {reason}")
                    if (
                        job_snapshot_key(job) != entry["job_snapshot"]
                        or _analysis_key(job) != entry["analysis_fingerprint"]
                    ):
                        raise StaleTaskResultError()
                    return job

                with session_scope() as session:
                    validate_source(session)
                if operation == "reextract_then_fit":
                    if (
                        plan["extraction_request"]["system"] != job_analysis.build_system_prompt()
                        or plan["extraction_request"]["schema"] != OfferAnalysis.model_json_schema()
                        or plan["extraction_request"]["max_completion_tokens"]
                        != get_settings().openai_max_completion_tokens
                    ):
                        raise StaleTaskResultError()
                    from smartapply.offers.analyzer_input import AnalyzerInput

                    analyzer = AnalyzerInput(**entry["analyzer_input"])
                    if entry["extraction_user"] != job_analysis.build_user_prompt_from_input(
                        analyzer_input=analyzer
                    ):
                        raise StaleTaskResultError()
                    offer = provider.complete_json(
                        system=plan["extraction_request"]["system"],
                        user=entry["extraction_user"],
                        schema=OfferAnalysis,
                        model=context["model"],
                        purpose="job_offer_analysis",
                        job_id=job_id,
                        temperature=plan["extraction_request"]["temperature"],
                        max_completion_tokens=plan["extraction_request"]["max_completion_tokens"],
                    )
                    offer = _ground_offer_excerpts(offer, analyzer.offer_body)
                    provenance = {"analyzer_input": entry["analyzer_input"], "operation": operation}
                else:
                    offer = OfferAnalysis.model_validate(entry["offer_facts"])
                    provenance = entry["extraction_provenance"]
                with session_scope() as session:
                    validate_source(session)
                fit, diagnostics = complete_fit(
                    provider, context, offer, job_id=job_id, extraction_provenance=provenance
                )
                with write_session_scope() as session:
                    job = validate_source(session)
                    raw = job.analysis.raw_response
                    # Preserve even historical offer fields; never publish invented defaults.
                    if not isinstance(raw, dict):
                        raise ValueError("Historical raw_response is not an exploitable object")
                    updated = {**raw, **fit.model_dump(mode="json")}
                    JobAnalysis.model_validate(updated)
                    job.analysis.fit_score = fit.fit_score
                    job.analysis.match_reasons = fit.match_reasons
                    job.analysis.risks = fit.risks
                    job.analysis.raw_response = updated
                    job.analysis.fit_diagnostics = diagnostics
                results.append({"job_id": job_id, "status": "rescored", "fit_score": fit.fit_score})
        except Exception as exc:
            results.append({"job_id": job_id, "status": "failed_or_stale", "error": str(exc)})
    return results
