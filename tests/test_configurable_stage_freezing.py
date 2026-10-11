"""Configured routes are frozen for a running fit, Batch and approved rescore."""

from __future__ import annotations

import copy

import pytest

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.llm.fit_context import build_fit_context, context_is_current
from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from smartapply.profile import get_profile
from tests.fit_fixtures import decision_response
from tests.llm_config_helpers import change_call, configure_calls
from tests.test_batch_analysis import FakeTransport, extraction_offer, journal, pipeline, seed_jobs
from tests.test_evidence_fit import seeded_analysis

PROVIDERS = [("openai", "gpt-5.4-mini"), ("anthropic", "claude-haiku-5-5")]


def routes(provider, model):
    return {
        "job_scope_screening": {
            "provider": provider, "model": model, "reasoning": "low",
            "max_output_tokens": 4096,
        },
        "job_offer_extraction": {
            "provider": provider, "model": model, "reasoning": "low",
            "max_output_tokens": 6000,
        },
        "job_fit_assessment": {
            "provider": provider, "model": model, "reasoning": "medium",
            "max_output_tokens": 12000,
        },
    }


@pytest.mark.parametrize(("provider_name", "model"), PROVIDERS)
def test_fit_context_uses_explicit_snapshot_after_json_changes(
    monkeypatch, tmp_path, provider_name, model
):
    path = configure_calls(monkeypatch, tmp_path, **routes(provider_name, model))
    settings = get_llm_settings()
    change_call(path, "job_fit_assessment", reasoning="high")
    context = build_fit_context(get_profile(), model, provider=provider_name, settings=settings)
    assert context["effort"] == "medium"
    assert context["max_completion_tokens"] == 12000
    assert context_is_current(context, get_profile())


@pytest.mark.parametrize(("provider_name", "model"), PROVIDERS)
def test_batch_start_freezes_all_three_explicit_routes(
    isolated_db, monkeypatch, tmp_path, provider_name, model
):
    path = configure_calls(monkeypatch, tmp_path, **routes(provider_name, model))
    transport = FakeTransport()
    transport.provider = provider_name
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    change_call(path, "job_fit_assessment", reasoning="high", max_output_tokens=16000)
    run_id = coordinator.start(seed_jobs())
    run, _, _ = journal(run_id)
    assert run.provider == provider_name and run.model == model
    assert run.extraction_effort == "low"
    assert run.fit_context["effort"] == "medium"
    assert run.fit_context["max_completion_tokens"] == 12000


@pytest.mark.parametrize(("provider_name", "model"), PROVIDERS)
def test_approved_rescore_uses_complete_prepared_routes_after_json_changes(
    isolated_db, monkeypatch, tmp_path, provider_name, model
):
    path = configure_calls(monkeypatch, tmp_path, **routes(provider_name, model))
    job_id = seeded_analysis()
    with session_scope() as session:
        session.get(Job, job_id).analysis.fit_diagnostics = {}
    plan = prepare_rescore_plan(job_ids=[job_id], include_extractions=True)
    assert plan["entries"][0]["operation"] == "reextract_then_fit"
    assert plan["extraction_request"]["effort"] == "low"
    assert plan["context"]["effort"] == "medium"
    frozen = copy.deepcopy(plan)
    change_call(path, "job_offer_extraction", reasoning="high", max_output_tokens=16000)
    change_call(path, "job_fit_assessment", reasoning="high", max_output_tokens=16000)
    calls = []

    class Provider:
        name = provider_name

        def complete_json(self, **kwargs):
            calls.append(kwargs)
            return extraction_offer() if kwargs["purpose"] == "job_offer_extraction" else decision_response()

    results = execute_rescore_plan(plan, Provider(), approved=True)
    assert results[0]["status"] == "rescored", results
    assert [(call["purpose"], call["effort"], call["max_completion_tokens"]) for call in calls] == [
        ("job_offer_extraction", "low", 6000),
        ("job_fit_assessment", "medium", 12000),
    ]
    assert plan == frozen
