"""Explicit effort survives settings changes and old journals stay unchanged."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.models import Job, LLMCache
from smartapply.llm.anthropic_batch import AnthropicBatchTransport
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.fit_context import build_fit_context, context_is_current
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.provider import LLMError
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
from smartapply.pipeline.batch_analysis.results import item_request
from smartapply.pipeline.process.analysis_checkpoint import extraction_key
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from smartapply.profile import get_profile
from tests.fit_fixtures import decision_response
from tests.llm_config_helpers import change_call, configure_calls
from tests.test_batch_analysis import extraction_offer, journal, offer, pipeline, seed_jobs
from tests.test_evidence_fit import seeded_analysis
from tests.test_provider_batch_analysis import NativeClaudeClient
from tests.test_two_stage_analysis import runner


@pytest.fixture(autouse=True)
def explicit_routes(monkeypatch, tmp_path):
    return configure_calls(
        monkeypatch,
        tmp_path,
        **{
            purpose: {
                "provider": "anthropic",
                "model": "claude-haiku-5-5",
                "reasoning": "high",
                "max_output_tokens": 32768,
            }
            for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment")
        },
    )


def _message(body, answer, *, stop_reason="end_turn"):
    return {
        "model": body["model"],
        "stop_reason": stop_reason,
        "content": [{"type": "text", "text": answer}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def test_frozen_fit_effort_survives_changed_effort_policy_and_transport_settings(explicit_routes):
    context = build_fit_context(get_profile(), "claude-haiku-5-5", provider="anthropic")
    frozen = copy.deepcopy(context)
    assert context["effort"] == "high"
    change_call(explicit_routes, "job_fit_assessment", reasoning="medium", max_output_tokens=18000)
    assert context_is_current(context, get_profile())
    assert context == frozen
    calls = []

    class Provider:
        name = "anthropic"

        def complete_json(self, **kwargs):
            calls.append(kwargs)
            return {**decision_response(), "issues": "malformed optional annotations"}

    result, diagnostics = complete_fit(Provider(), context, offer())
    assert len(calls) == 1 and calls[0]["effort"] == "high"
    assert result.fit_score == 0.95
    assert diagnostics["diagnostic_checks"]["warnings"]


def test_immediate_freezes_both_efforts_before_extraction_and_uses_batch_cache_identity(
    isolated_db, monkeypatch, explicit_routes
):
    job_id = seed_jobs()[0]
    provider = AnthropicProvider(model="claude-haiku-5-5")
    calls = []

    def call(**body):
        calls.append(copy.deepcopy(body))
        name = body["output_config"]["format"]["schema"]["title"]
        if name == "OfferExtractionOutput":
            for purpose in ("job_offer_extraction", "job_fit_assessment"):
                change_call(
                    explicit_routes,
                    purpose,
                    reasoning="medium",
                    model="claude-sonnet-5-5",
                    max_output_tokens=1024,
                )
            return _message(body, extraction_offer().model_dump_json())
        # Truncated annotations are kept as observations; no repair is bought.
        content = json.dumps(decision_response())[:-1] + ',"neutral_offer_re'
        return _message(body, content, stop_reason="max_tokens")

    monkeypatch.setattr(provider, "_call_anthropic", call)
    report = runner(provider).analyze_jobs([job_id])
    assert report.analyzed == 1 and not report.errors
    assert len(calls) == 2
    assert [body["output_config"]["effort"] for body in calls] == ["high", "high"]
    assert all(body["model"] == "claude-haiku-5-5" for body in calls)
    assert all(body["max_tokens"] == 32768 for body in calls)
    with session_scope() as session:
        job = session.get(Job, job_id)
        diagnostics = job.analysis.fit_diagnostics
        assert job.analysis.fit_score == 0.95
        assert diagnostics["diagnostic_checks"]["warnings"]
        assert diagnostics["context"]["effort"] == "high"
        assert diagnostics["extraction_provenance"]["effort"] == "high"
        run = AnalysisBatchRun(
            id="same-contract",
            provider="anthropic",
            model="claude-haiku-5-5",
            extraction_effort="high",
            max_completion_tokens=32768,
            profile_data=get_profile().model_dump(mode="json"),
            fit_context=diagnostics["context"],
            scope_context={"provider": "anthropic"},
        )
        item = AnalysisBatchItem(
            id=1,
            run_id=run.id,
            job_id=job_id,
            analyzer_input=asdict(build_analyzer_input(job)),
            offer_data=extraction_offer().model_dump(mode="json"),
        )
        requests = [item_request(item, run, phase, 32768) for phase in ("extraction", "evaluation")]
        assert {record.cache_key for record in requests} == {
            cached.cache_key for cached in session.scalars(select(LLMCache))
        }
        assert diagnostics["extraction_provenance"]["extraction_key"] == requests[0].cache_key


def test_immediate_extraction_checkpoint_is_reused_by_the_identical_batch(isolated_db, monkeypatch):
    job_id = seed_jobs()[0]
    provider = AnthropicProvider(model="claude-haiku-5-5")
    calls = []

    def call(**body):
        calls.append(body)
        if len(calls) == 1:
            return _message(body, extraction_offer().model_dump_json())
        raise LLMError("Simulated fit failure; retain the completed extraction")

    monkeypatch.setattr(provider, "_call_anthropic", call)
    report = runner(provider).analyze_jobs([job_id])
    assert report.analyzed == 0 and len(report.errors) == 1
    with session_scope() as session:
        job = session.get(Job, job_id)
        checkpoint = session.get(AnalysisCheckpoint, job_id)
        assert checkpoint.extraction_key == extraction_key(
            build_analyzer_input(job), "claude-haiku-5-5", provider="anthropic", effort="high"
        )
    native = NativeClaudeClient()
    coordinator = BatchAnalysisCoordinator(
        transport=AnthropicBatchTransport(native), pipeline=pipeline()
    )
    run_id = coordinator.start([job_id])
    assert journal(run_id)[1][0].state == "evaluation_pending"
    assert native.created == ["msgbatch-1"]
    assert all("_evaluation-" in request["custom_id"] for request in native.inputs["msgbatch-1"])
    native.complete("msgbatch-1")
    coordinator.sync()
    assert journal(run_id)[0].state == "completed"
    assert native.created == ["msgbatch-1"]


@pytest.mark.parametrize("expected", ["medium", "high"])
def test_batch_keeps_configured_effort_across_both_phases(isolated_db, explicit_routes, expected):
    for purpose in ("job_offer_extraction", "job_fit_assessment"):
        change_call(explicit_routes, purpose, reasoning=expected)
    ids = seed_jobs()
    native = NativeClaudeClient()
    coordinator = BatchAnalysisCoordinator(
        transport=AnthropicBatchTransport(native), pipeline=pipeline()
    )
    run_id = coordinator.start(ids)
    frozen_run, _, waves = journal(run_id)
    original_requests = copy.deepcopy(waves[0].requests)
    original_context = copy.deepcopy(frozen_run.fit_context)
    assert frozen_run.extraction_effort == expected
    assert frozen_run.fit_context.get("effort") == expected
    assert native.inputs["msgbatch-1"][0]["params"]["output_config"].get("effort") == expected
    for purpose in ("job_offer_extraction", "job_fit_assessment"):
        change_call(
            explicit_routes,
            purpose,
            model="claude-sonnet-5-5",
            reasoning="low",
            max_output_tokens=1024,
        )
    native.complete("msgbatch-1")
    coordinator.sync()
    assert journal(run_id)[2][0].requests == original_requests
    assert journal(run_id)[0].fit_context == original_context
    assert native.inputs["msgbatch-2"][0]["params"]["output_config"].get("effort") == expected
    native.complete("msgbatch-2")
    coordinator.sync()
    assert journal(run_id)[0].state == "completed"
    assert len(native.created) == 2


def test_explicit_rescore_plan_freezes_extraction_and_fit_effort(
    isolated_db, monkeypatch, explicit_routes
):
    job_id = seeded_analysis()
    with session_scope() as session:
        # No recorded source provenance: extraction must be explicitly included
        # in the approved plan; no facts are reconstructed from the old score.
        session.get(Job, job_id).analysis.fit_diagnostics = {}
    plan = prepare_rescore_plan(job_ids=[job_id], include_extractions=True)
    assert plan["entries"][0]["operation"] == "reextract_then_fit"
    assert plan["extraction_request"]["effort"] == plan["context"]["effort"] == "high"
    frozen = copy.deepcopy(plan)
    for purpose in ("job_offer_extraction", "job_fit_assessment"):
        change_call(explicit_routes, purpose, reasoning="medium")
    calls = []

    class Provider:
        name = "anthropic"

        def complete_json(self, **kwargs):
            calls.append(kwargs)
            return (
                extraction_offer()
                if kwargs["purpose"] == "job_offer_extraction"
                else decision_response()
            )

    results = execute_rescore_plan(plan, Provider(), approved=True)
    assert results[0]["status"] == "rescored"
    assert len(calls) == 2
    assert [call["effort"] for call in calls] == ["high", "high"]
    assert plan == frozen
