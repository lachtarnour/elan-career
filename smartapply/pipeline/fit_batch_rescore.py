"""Explicit fit-only Batch journal; completed applications keep their workflow.

Rescore states deliberately differ from ordinary analysis states: an older
installed app must not advance this journal as a new-offer analysis. All paid
requests, frozen inputs and consumed results live in the existing Batch tables.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, text

from smartapply.concurrency import job_operation
from smartapply.config import get_llm_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.analysis_batches import (
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.models import Job
from smartapply.database.repository.analyses import set_analysis
from smartapply.database.repository.llm_cache import cache_set, record_usage
from smartapply.llm.batch import (
    BatchRequest,
    build_batch_request,
    encode_requests,
    get_batch_transport,
)
from smartapply.llm.fit_context import build_fit_user, context_is_current, fingerprint
from smartapply.llm.fit_request import check_request_budget
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm.schemas import (
    JobAnalysis,
    JobFitAssessment,
    OfferAnalysis,
    OfferExtractionOutput,
)
from smartapply.llm.usage import estimate_cost_usd
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.batch_analysis.locking import batch_operation
from smartapply.pipeline.batch_analysis.results import validate_request_response
from smartapply.pipeline.process.analysis import _assemble_analysis, _ground_offer_excerpts
from smartapply.pipeline.process.extraction_screening import extraction_offer_facts
from smartapply.pipeline.rescore import _analysis_key
from smartapply.pipeline.snapshots import job_is_active, job_snapshot_key
from smartapply.profile import get_profile


def now():
    return datetime.now(timezone.utc)


def analysis_key(job):
    return _analysis_key(job) if job.analysis is not None else fingerprint(None)


class FitBatchRescore:
    """No immediate completion path and no automatic duplicate submission."""

    def __init__(self, *, transport=None, settings=None):
        self.settings = settings or get_llm_settings()
        self.transport = transport

    def _transport(self, run):
        return self.transport or get_batch_transport(run.provider, settings=self.settings)

    def prepare(self, plan: dict, *, selected_ids: list[int]) -> str:
        unsigned = {key: value for key, value in plan.items() if key != "plan_fingerprint"}
        if fingerprint(unsigned) != plan["plan_fingerprint"]:
            raise ValueError("Modified rescore plan")
        if not context_is_current(plan["context"], get_profile()):
            raise ValueError("Profile or scoring contract changed")
        context = plan["context"]
        extraction = plan["extraction_request"]
        if context["provider"] != extraction["provider"] or context["model"] != extraction["model"]:
            raise ValueError("This mixed Batch requires the same frozen extraction and fit route")
        entries = {entry["job_id"]: entry for entry in plan["entries"]}
        if set(selected_ids) != set(entries) or len(selected_ids) != len(set(selected_ids)):
            raise ValueError("Selected offers differ from the approved scope")
        run_id = str(uuid4())
        with (
            batch_operation(),
            job_operation(selected_ids, operation="préparation réévaluation Batch"),
            write_session_scope() as session,
        ):
            session.execute(text("BEGIN IMMEDIATE"))
            run = AnalysisBatchRun(
                id=run_id,
                state="rescore_active",
                provider=context["provider"],
                model=extraction["model"],
                max_completion_tokens=extraction["max_completion_tokens"],
                extraction_effort=extraction.get("effort"),
                profile_data=get_profile().model_dump(mode="json"),
                fit_context=copy.deepcopy(context),
                scope_context={
                    "mode": "fit_rescore",
                    "extraction_request": copy.deepcopy(extraction),
                    "plan_fingerprint": plan["plan_fingerprint"],
                },
            )
            session.add(run)
            for job_id in selected_ids:
                job = session.get(Job, job_id)
                entry = entries[job_id]
                if (
                    job is None
                    or not job_is_active(job)
                    or job.status not in {"analyzed", "ready_for_form_submission"}
                ):
                    raise ValueError(f"Offer {job_id} is no longer eligible")
                if job.application and job.application.form_submitted_at is not None:
                    raise ValueError(f"Offer {job_id} was submitted")
                if entry["operation"] == "evaluate_fit":
                    if (
                        job_snapshot_key(job) != entry["job_snapshot"]
                        or analysis_key(job) != entry["analysis_fingerprint"]
                    ):
                        raise ValueError(f"Offer {job_id} changed after planning")
                    facts, provenance = entry["offer_facts"], entry["extraction_provenance"]
                elif (
                    entry["operation"] == "excluded"
                    and entry["reason"] == "no_successful_fit"
                    and job.analysis is None
                ):
                    facts, provenance = None, None
                else:
                    raise ValueError(
                        f"Unsupported or protected offer {job_id}: {entry['operation']}"
                    )
                session.add(
                    AnalysisBatchItem(
                        run_id=run_id,
                        job_id=job_id,
                        job_snapshot=job_snapshot_key(job),
                        title=job.title,
                        company=job.source_company,
                        analyzer_input=asdict(build_analyzer_input(job)),
                        offer_data=copy.deepcopy(facts),
                        scope_data={
                            "analysis_fingerprint": analysis_key(job),
                            "extraction_provenance": copy.deepcopy(provenance),
                        },
                        state="evaluation_pending" if facts else "extraction_pending",
                    )
                )
            session.flush()
            self._prepare_wave(session, run)
        return run_id

    def _request(self, item, run):
        if item.state == "evaluation_pending":
            phase, context, schema = "evaluation", run.fit_context, JobFitAssessment
            offer = OfferAnalysis.model_validate(item.offer_data)
            user = build_fit_user(
                context, offer, extraction_provenance=item.scope_data["extraction_provenance"]
            )
            check_request_budget(context, user)
            system, model, budget, temperature, effort = (
                context["system_prompt"],
                context["model"],
                context["max_completion_tokens"],
                context["temperature"],
                context.get("effort"),
            )
        else:
            from smartapply.llm.prompts import job_analysis
            from smartapply.offers.analyzer_input import AnalyzerInput

            phase, schema = "extraction", OfferExtractionOutput
            route = run.scope_context["extraction_request"]
            system, model, budget, temperature, effort = (
                route["system"],
                route["model"],
                route["max_completion_tokens"],
                route["temperature"],
                route.get("effort"),
            )
            user = job_analysis.build_user_prompt_from_input(
                analyzer_input=AnalyzerInput(**item.analyzer_input)
            )
        request = build_batch_request(
            custom_id=f"{phase[0]}{item.job_id}",
            system=system,
            user=user,
            schema=schema,
            model=model,
            max_completion_tokens=budget,
            temperature=temperature,
            effort=effort,
            provider=run.provider,
            purpose="job_fit_assessment" if phase == "evaluation" else "job_offer_extraction",
        )
        return {
            "custom_id": request.custom_id,
            "body": request.body,
            "cache_key": request.cache_key,
            "item_ids": [item.id],
            "phase": phase,
        }

    def _prepare_wave(self, session, run):
        existing = session.scalars(
            select(AnalysisBatchWave).where(
                AnalysisBatchWave.run_id == run.id, AnalysisBatchWave.state != "imported"
            )
        ).first()
        if existing is not None:
            return existing
        items = list(
            session.scalars(select(AnalysisBatchItem).where(AnalysisBatchItem.run_id == run.id))
        )
        records = []
        for item in items:
            if item.state in {"evaluation_pending", "extraction_pending"}:
                records.append(self._request(item, run))
        if not records:
            return None
        # Validate the complete canonical input before committing submission intent.
        encode_requests([BatchRequest(r["custom_id"], r["body"], r["cache_key"]) for r in records])
        wave = AnalysisBatchWave(
            id=str(uuid4()), run_id=run.id, phase="evaluation", requests=records, state="prepared"
        )
        session.add(wave)
        return wave

    def _check_job(self, session, item):
        job = session.get(Job, item.job_id)
        if job is None or not job_is_active(job) or job_snapshot_key(job) != item.job_snapshot:
            raise ValueError(f"Offer {item.job_id} changed during the Batch")
        if analysis_key(job) != item.scope_data["analysis_fingerprint"]:
            raise ValueError(f"Analysis {item.job_id} changed during the Batch")
        if job.application and job.application.form_submitted_at is not None:
            raise ValueError(f"Offer {item.job_id} was submitted during the Batch")
        return job

    def submit(self, run_id):
        with batch_operation():
            with session_scope() as session:
                run = session.get(AnalysisBatchRun, run_id)
                if run is None or run.scope_context.get("mode") != "fit_rescore":
                    raise ValueError("Not a fit rescore journal")
                if not context_is_current(run.fit_context, get_profile()):
                    raise ValueError("Profile or scoring contract changed")
                wave = session.scalars(
                    select(AnalysisBatchWave).where(
                        AnalysisBatchWave.run_id == run_id, AnalysisBatchWave.state == "prepared"
                    )
                ).one_or_none()
                if wave is None:
                    return self.status(run_id)
                wave_id = wave.id
                requests = [
                    BatchRequest(r["custom_id"], r["body"], r["cache_key"]) for r in wave.requests
                ]
                transport = self._transport(run)
                for record in wave.requests:
                    self._check_job(session, session.get(AnalysisBatchItem, record["item_ids"][0]))
            reference = transport.upload(encode_requests(requests))
            with write_session_scope() as session:
                wave = session.get(AnalysisBatchWave, wave_id)
                if wave.state != "prepared":
                    raise ValueError("Submission state changed")
                wave.input_file_id = reference
                wave.state = "submitting"
                wave.provider_data = {"submission_intent_at": now().isoformat()}
            # No retry of this POST. An unknown outcome remains submitting.
            try:
                snapshot = transport.create(
                    reference,
                    {"elan_run_id": run_id, "elan_wave_id": wave_id},
                    **(
                        {"requests": requests}
                        if getattr(transport, "requires_journal", False)
                        else {}
                    ),
                )
            except Exception as exc:
                with write_session_scope() as session:
                    run = session.get(AnalysisBatchRun, run_id)
                    run.state = "rescore_attention"
                    run.last_error = str(exc)
                raise
            with write_session_scope() as session:
                wave = session.get(AnalysisBatchWave, wave_id)
                wave.provider_batch_id = snapshot.id
                wave.state = snapshot.status
                wave.provider_data = {**wave.provider_data, **asdict(snapshot)}
                run = session.get(AnalysisBatchRun, run_id)
                run.state = "rescore_active"
                run.last_error = ""
            return self.status(run_id)

    def import_wave(self, wave_id, results):
        """Scores, history, cache, usage and consumed IDs commit together."""
        with session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            ids = [
                r["item_ids"][0] for r in wave.requests if r["custom_id"] not in wave.consumed_ids
            ]
            job_ids = [session.get(AnalysisBatchItem, i).job_id for i in ids]
        with (
            job_operation(job_ids, operation="publication réévaluation Batch"),
            write_session_scope() as session,
        ):
            session.execute(text("BEGIN IMMEDIATE"))
            wave = session.get(AnalysisBatchWave, wave_id)
            if wave.state == "imported":
                return
            run = session.get(AnalysisBatchRun, wave.run_id)
            if not context_is_current(run.fit_context, get_profile()):
                raise ValueError("Profile or scoring contract changed; retain paid responses")
            if set(results) != {r["custom_id"] for r in wave.requests}:
                raise ValueError("Batch response IDs differ from the frozen requests")
            consumed = list(wave.consumed_ids)
            for record in wave.requests:
                custom_id = record["custom_id"]
                if custom_id in consumed:
                    continue
                result = results[custom_id]
                item = session.get(AnalysisBatchItem, record["item_ids"][0])
                phase = record["phase"]
                body = record["body"]
                served = result.model or body["model"]
                record_usage(
                    session,
                    purpose="job_fit_assessment"
                    if phase == "evaluation"
                    else "job_offer_extraction",
                    model=served,
                    requested_model=body["model"],
                    provider=run.provider,
                    reasoning_effort=body.get("output_config", {}).get("effort"),
                    job_id=item.job_id,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    cached_prompt_tokens=result.cached_prompt_tokens,
                    cache_write_prompt_tokens=result.cache_write_prompt_tokens,
                    cost_usd=estimate_cost_usd(
                        served,
                        result.prompt_tokens,
                        result.completion_tokens,
                        cached_prompt_tokens=result.cached_prompt_tokens,
                        cache_write_prompt_tokens=result.cache_write_prompt_tokens,
                    )
                    * 0.5,
                    cache_key=record["cache_key"],
                    cached=False,
                )
                try:
                    schema = JobFitAssessment if phase == "evaluation" else OfferExtractionOutput
                    result.validate(schema)
                    validated = validate_request_response(body, result.content)
                except ValueError as exc:
                    item.state = f"{phase}_failed"
                    item.error = str(exc)
                    consumed.append(custom_id)
                    continue
                except Exception as exc:
                    from smartapply.llm.provider import LLMValidationError

                    if not isinstance(exc, LLMValidationError):
                        raise
                    item.state = f"{phase}_failed"
                    item.error = str(exc)
                    consumed.append(custom_id)
                    continue
                cache_set(
                    session,
                    provider=run.provider,
                    cache_key=record["cache_key"],
                    model=body["model"],
                    response=result.content,
                    purpose="job_fit_assessment"
                    if phase == "evaluation"
                    else "job_offer_extraction",
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                )
                job = self._check_job(session, item)
                if phase == "extraction":
                    offer = _ground_offer_excerpts(
                        extraction_offer_facts(validated), item.analyzer_input["offer_body"]
                    )
                    item.offer_data = offer.model_dump(mode="json")
                    data = copy.deepcopy(item.scope_data)
                    data["extraction_provenance"] = {
                        "analyzer_input": item.analyzer_input,
                        "operation": "batch_extraction",
                        "provider": run.provider,
                        "model": served,
                        "batch_id": wave.provider_batch_id,
                        "response_id": result.response_id,
                    }
                    item.scope_data = data
                    item.state = "evaluation_pending"
                else:
                    offer = OfferAnalysis.model_validate(item.offer_data)
                    fit, diagnostics = evaluate_fit(
                        validated,
                        run.fit_context,
                        offer,
                        extraction_provenance=item.scope_data["extraction_provenance"],
                    )
                    public = _assemble_analysis(offer, fit)
                    diagnostics.update(
                        request_user=body["messages"][1]["content"],
                        request_budget=check_request_budget(
                            run.fit_context, body["messages"][1]["content"]
                        ),
                        batch_provenance={
                            "run_id": run.id,
                            "wave_id": wave.id,
                            "batch_id": wave.provider_batch_id,
                            "custom_id": custom_id,
                            "response_id": result.response_id,
                            "requested_model": body["model"],
                            "served_model": served,
                        },
                    )
                    if job.analysis is not None:
                        raw = {
                            **job.analysis.raw_response,
                            **fit.model_dump(mode="json"),
                            "risks": public.risks,
                        }
                        JobAnalysis.model_validate(raw)
                        set_analysis(
                            session,
                            job.id,
                            fit_score=fit.fit_score,
                            match_reasons=fit.match_reasons,
                            risks=public.risks,
                            raw_response=raw,
                            fit_diagnostics=diagnostics,
                        )
                    else:
                        set_analysis(
                            session,
                            job.id,
                            role_type=public.role_type,
                            seniority=public.seniority,
                            domain=public.domain,
                            main_tasks=public.main_tasks,
                            required_skills=public.required_skills,
                            nice_to_have=public.nice_to_have,
                            cv_keywords_to_include=public.cv_keywords_to_include,
                            fit_score=fit.fit_score,
                            match_reasons=fit.match_reasons,
                            risks=public.risks,
                            raw_response=public.model_dump(mode="json"),
                            fit_diagnostics=diagnostics,
                            model_used=body["model"],
                        )
                    item.state = "completed"
                consumed.append(custom_id)
            wave.consumed_ids = consumed
            wave.state = "imported"
            session.flush()
            pending = self._prepare_wave(session, run)
            if pending is None:
                items = list(
                    session.scalars(
                        select(AnalysisBatchItem).where(AnalysisBatchItem.run_id == run.id)
                    )
                )
                run.state = (
                    "completed"
                    if all(i.state == "completed" for i in items)
                    else "completed_with_errors"
                )
                run.completed_at = now()
            run.updated_at = now()

    def sync(self, run_id):
        import json
        from pathlib import Path

        from smartapply.config import get_settings

        with batch_operation():
            with session_scope() as session:
                run = session.get(AnalysisBatchRun, run_id)
                transport = self._transport(run)
                waves = list(
                    session.scalars(
                        select(AnalysisBatchWave).where(
                            AnalysisBatchWave.run_id == run_id,
                            AnalysisBatchWave.state != "imported",
                        )
                    )
                )
            for wave in waves:
                if wave.state == "prepared":
                    continue
                if not wave.provider_batch_id:
                    requests = [
                        BatchRequest(r["custom_id"], r["body"], r["cache_key"])
                        for r in wave.requests
                    ]
                    matches = transport.recover(
                        {"elan_run_id": run_id, "elan_wave_id": wave.id},
                        **(
                            {
                                "requests": requests,
                                "created_after": datetime.fromisoformat(
                                    wave.provider_data["submission_intent_at"]
                                ),
                            }
                            if getattr(transport, "requires_journal", False)
                            else {}
                        ),
                    )
                    if len(matches) != 1:
                        continue
                    snapshot = matches[0]
                else:
                    snapshot = transport.retrieve(wave.provider_batch_id)
                with write_session_scope() as session:
                    saved = session.get(AnalysisBatchWave, wave.id)
                    saved.provider_batch_id = snapshot.id
                    saved.provider_data = {**saved.provider_data, **asdict(snapshot)}
                    saved.state = snapshot.status
                if snapshot.terminal:
                    results = transport.download(
                        snapshot.output_file_id,
                        **(
                            {"metadata": {"elan_wave_id": wave.id}}
                            if getattr(transport, "requires_journal", False)
                            else {}
                        ),
                    )
                    # Save paid response evidence before attempting any publication.
                    output = Path(get_settings().cache_dir) / "fit-batch-results" / run_id
                    output.mkdir(parents=True, exist_ok=True, mode=0o700)
                    (output / f"{wave.id}.json").write_text(
                        json.dumps({k: asdict(v) for k, v in results.items()}, ensure_ascii=False)
                    )
                    self.import_wave(wave.id, results)
        return self.status(run_id)

    def status(self, run_id):
        from collections import Counter

        with session_scope() as session:
            run = session.get(AnalysisBatchRun, run_id)
            items = list(
                session.scalars(select(AnalysisBatchItem).where(AnalysisBatchItem.run_id == run_id))
            )
            waves = list(
                session.scalars(select(AnalysisBatchWave).where(AnalysisBatchWave.run_id == run_id))
            )
            return {
                "run_id": run_id,
                "state": run.state,
                "items": dict(Counter(i.state for i in items)),
                "waves": [
                    {
                        "wave_id": w.id,
                        "state": w.state,
                        "batch_id": w.provider_batch_id,
                        "requests": len(w.requests),
                        "consumed": len(w.consumed_ids),
                        "provider_counts": {
                            key: w.provider_data.get(key)
                            for key in ("total", "completed", "failed")
                        },
                    }
                    for w in waves
                ],
            }
