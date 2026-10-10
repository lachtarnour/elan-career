"""Real independent stages against isolated data and simulated providers."""

from __future__ import annotations

import copy
import itertools
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.models import Job, LLMCache, LLMUsage
from smartapply.database.scope_checkpoints import ScopeCheckpoint
from smartapply.desktop.llm_settings import llm_configuration, save_llm_calls
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.factory import get_llm_provider
from smartapply.llm.openai_provider import OpenAIProvider
from smartapply.llm.prompts import job_analysis, job_scope_screening
from smartapply.llm.provider import LLMError
from smartapply.llm.schemas import (
    JobScopeScreening,
    OfferExtractionOutput,
)
from smartapply.llm.structured import Completion
from smartapply.llm_policy import resolve_route
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
from smartapply.pipeline.errors import TaskCancelledError
from smartapply.pipeline.process.scope_screening import scope_context, scope_request
from smartapply.pipeline.snapshots import CancellableProvider
from tests.fit_fixtures import evaluation_from_user
from tests.scope_fixtures import set_test_routes
from tests.test_batch_analysis import FakeTransport, journal, pipeline, seed_jobs
from tests.test_extraction_mission_screen import _offer
from tests.test_two_stage_analysis import runner


@pytest.fixture(autouse=True)
def reference_routes(monkeypatch, tmp_path):
    return set_test_routes(
        monkeypatch,
        tmp_path,
        {
            purpose: {
                "provider": provider,
                "model": model,
                "reasoning": reasoning,
                "max_output_tokens": tokens,
            }
            for purpose, provider, model, reasoning, tokens in (
                ("job_scope_screening", "anthropic", "claude-haiku-5-5", "high", 4096),
                ("job_offer_extraction", "openai", "gpt-5.4-mini", "medium", 6000),
                ("job_fit_assessment", "openai", "gpt-5.4-mini", "medium", 12000),
            )
        },
    )


def verdict(relation="unknown", evidence="insufficient"):
    return JobScopeScreening.model_validate(
        {
            "mission_screen": {
                "mission_relation": relation,
                "mission_evidence": evidence,
            }
        }
    )


def factual_offer():
    return OfferExtractionOutput.model_validate(_offer().model_dump(exclude={"mission_screen"}))


def simulated_provider(calls, screen=None, *, fit_error=False):
    def complete(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if kwargs["purpose"] == "job_scope_screening":
            if isinstance(screen, Exception):
                raise screen
            return screen if screen is not None else verdict()
        if kwargs["purpose"] == "job_offer_extraction":
            assert kwargs["schema"] is OfferExtractionOutput
            assert "mission_screen" not in kwargs["system"]
            assert "mission_relation" not in kwargs["user"]
            return factual_offer()
        if fit_error:
            raise LLMError("fit unavailable")
        return evaluation_from_user(kwargs["user"])

    settings = get_llm_settings()

    def for_purpose(purpose):
        route = resolve_route(settings, purpose)
        return SimpleNamespace(
            name=route.provider,
            cheap_model=route.model,
            _settings=settings,
            max_completion_tokens=route.max_output_tokens,
            complete_json=complete,
        )

    return SimpleNamespace(for_purpose=for_purpose)


def test_configuration_lists_and_saves_both_calls_independently(reference_routes):
    before = {call["purpose"]: call for call in llm_configuration()["calls"]}
    assert before["job_scope_screening"]["model"] == "claude-haiku-5-5"
    save_llm_calls(
        [
            {
                "purpose": "job_scope_screening",
                "provider": "anthropic",
                "model": "claude-sonnet-5-5",
                "reasoning": "high",
                "max_output_tokens": 8192,
            },
            {
                "purpose": "job_offer_extraction",
                "provider": "openai",
                "model": "gpt-5.4",
                "reasoning": "medium",
                "max_output_tokens": 6000,
            },
        ]
    )
    after = {call["purpose"]: call for call in llm_configuration()["calls"]}
    assert after["job_scope_screening"]["model"] == "claude-sonnet-5-5"
    assert after["job_offer_extraction"]["model"] == "gpt-5.4"
    assert after["job_fit_assessment"] == before["job_fit_assessment"]


@pytest.mark.parametrize(
    "relation,evidence",
    list(
        itertools.product(
            ("direct", "adjacent", "clearly_outside", "unknown"),
            ("sufficient", "insufficient", "contradictory"),
        )
    ),
)
def test_gate_precedes_extraction_and_never_transmits_verdict(isolated_db, relation, evidence):
    job_id = seed_jobs(screened=False)[0]
    calls = []
    analyzed = runner(simulated_provider(calls, verdict(relation, evidence)))._analyze_one(
        job_id=job_id
    )
    rejected = relation == "clearly_outside" and evidence == "sufficient"
    assert analyzed is not rejected
    assert [call["purpose"] for call in calls] == (
        ["job_scope_screening"]
        if rejected
        else ["job_scope_screening", "job_offer_extraction", "job_fit_assessment"]
    )
    assert "CANDIDATE" not in calls[0]["user"]
    assert "SCRAPER METADATA" not in calls[0]["user"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert bool(job.archived_at) is rejected
        assert (job.analysis is None) is rejected
        assert session.scalar(select(ScopeCheckpoint).where(ScopeCheckpoint.job_id == job_id))
        if rejected:
            assert session.get(AnalysisCheckpoint, job_id) is None


@pytest.mark.parametrize(
    "screen",
    [
        LLMError("API unavailable"),
        {},
        {"mission_screen": {"mission_relation": "invalid", "mission_evidence": "sufficient"}},
    ],
)
def test_invalid_or_failed_screen_is_checkpointed_and_not_repeated_on_fit_retry(
    isolated_db, screen
):
    job_id = seed_jobs(screened=False)[0]
    calls = []
    with pytest.raises(LLMError, match="fit unavailable"):
        runner(simulated_provider(calls, screen, fit_error=True))._analyze_one(job_id=job_id)
    assert runner(simulated_provider(calls, screen))._analyze_one(job_id=job_id)
    assert [call["purpose"] for call in calls] == [
        "job_scope_screening",
        "job_offer_extraction",
        "job_fit_assessment",
        "job_fit_assessment",
    ]
    with session_scope() as session:
        saved = session.scalar(select(ScopeCheckpoint).where(ScopeCheckpoint.job_id == job_id))
        assert saved.result_data["status"] == "fail_open"
        assert session.get(Job, job_id).archived_at is None


def test_cancellation_after_screen_retains_checkpoint_before_extraction(isolated_db):
    job_id = seed_jobs(screened=False)[0]
    calls, stop = [], {"value": False}
    provider = simulated_provider(calls)
    original = provider.for_purpose("job_scope_screening").complete_json

    def complete(**kwargs):
        result = original(**kwargs)
        if kwargs["purpose"] == "job_scope_screening":
            stop["value"] = True
        return result

    provider.for_purpose = lambda purpose: provider
    provider.name = "openai"
    provider.cheap_model = "mock"
    provider.complete_json = complete
    instance = runner(CancellableProvider(provider, lambda: stop["value"]))
    instance.stop_requested = lambda: stop["value"]
    with pytest.raises(TaskCancelledError):
        instance._analyze_one(job_id=job_id)
    stop["value"] = False
    assert instance._analyze_one(job_id=job_id)
    assert [call["purpose"] for call in calls] == [
        "job_scope_screening",
        "job_offer_extraction",
        "job_fit_assessment",
    ]


def test_provider_caches_and_usage_are_separate_with_reference_routes(isolated_db, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    calls = []

    def generate(provider, body):
        name = body["response_format"]["json_schema"]["name"]
        calls.append((provider.name, copy.deepcopy(body)))
        answer = (
            verdict()
            if name == "JobScopeScreening"
            else factual_offer()
            if name == "OfferExtractionOutput"
            else evaluation_from_user(body["messages"][1]["content"])
        )
        return Completion(
            content=answer.model_dump_json(),
            model=body["model"],
            prompt_tokens=10,
            completion_tokens=10,
        )

    monkeypatch.setattr(AnthropicProvider, "_generate", generate)
    monkeypatch.setattr(OpenAIProvider, "_generate", generate)
    job_id = seed_jobs(screened=False)[0]
    assert runner(get_llm_provider())._analyze_one(job_id=job_id)
    assert [provider for provider, _ in calls] == ["anthropic", "openai", "openai"]
    assert calls[0][1]["output_config"]["effort"] == "high"
    assert calls[1][1]["reasoning_effort"] == "medium"
    assert calls[1][1]["response_format"]["json_schema"]["name"] == "OfferExtractionOutput"
    with session_scope() as session:
        caches = list(session.scalars(select(LLMCache)))
        assert {entry.purpose for entry in caches} == {
            "job_scope_screening",
            "job_offer_extraction",
            "job_fit_assessment",
        }
        assert len({entry.cache_key for entry in caches}) == 3
        usage = {entry.purpose: entry for entry in session.scalars(select(LLMUsage))}
        assert usage["job_scope_screening"].provider == "anthropic"
        assert usage["job_offer_extraction"].provider == "openai"


def test_scope_identity_changes_with_prompt_source_provider_model_and_effort(isolated_db):
    job_id = seed_jobs(screened=False)[0]
    with session_scope() as session:
        analyzer = build_analyzer_input(session.get(Job, job_id))
    settings = get_llm_settings()
    context = scope_context(
        provider="anthropic",
        model="claude-haiku-5-5",
        settings=settings,
        max_completion_tokens=32768,
    )
    keys = {scope_request(context, analyzer).cache_key}
    for update in (
        {"system_prompt": context["system_prompt"] + "\n"},
        {"model": "claude-sonnet-5-5"},
        {"effort": "medium"},
        {"provider": "openai", "model": "gpt-5.4-mini"},
    ):
        keys.add(scope_request(context | update, analyzer).cache_key)
    from dataclasses import replace

    keys.add(
        scope_request(
            context, replace(analyzer, offer_body=analyzer.offer_body + " changed")
        ).cache_key
    )
    assert len(keys) == 6
    assert "mission_screen" not in job_analysis.build_system_prompt()
    assert "mission_screen" in job_scope_screening.build_system_prompt()


def test_batch_reference_routes_wait_for_screening_and_extract_only_kept_offers(
    isolated_db, monkeypatch
):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    ids = seed_jobs(2, screened=False)
    transports = {"anthropic": FakeTransport(batch_prefix="scope"), "openai": FakeTransport()}
    coordinator = BatchAnalysisCoordinator(pipeline=pipeline())
    coordinator._transports = transports
    run_id = coordinator.start(ids)
    screen = transports["anthropic"]
    assert not transports["openai"].created
    records = screen.requests("scope-1")
    assert all(record["body"]["model"] == "claude-haiku-5-5" for record in records)
    rejected = records[1]["custom_id"]
    screen.complete(
        "scope-1",
        answers={
            record["custom_id"]: verdict("clearly_outside", "sufficient")
            if record["custom_id"] == rejected
            else verdict("adjacent", "sufficient")
            for record in records
        },
    )
    coordinator.sync()
    extraction = transports["openai"].requests("batch-1")
    assert len(extraction) == 1
    assert extraction[0]["body"]["model"] == "gpt-5.4-mini"
    assert extraction[0]["body"]["reasoning_effort"] == "medium"
    assert "mission_screen" not in str(extraction[0]["body"])
    transports["openai"].complete("batch-1")
    coordinator.sync()
    transports["openai"].complete("batch-2")
    coordinator.sync()
    coordinator.sync()
    assert journal(run_id)[0].state == "completed"
    assert [item.state for item in journal(run_id)[1]] == ["completed", "filtered_after_screening"]
    assert screen.created == ["scope-1"]
    assert len(transports["openai"].created) == 2


@pytest.mark.parametrize(
    "failure", ["upload", "create", "retrieve", "invalid", "missing", "provider_error"]
)
def test_batch_screen_errors_fail_open_without_another_screen_request(
    isolated_db, monkeypatch, failure
):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    ids = seed_jobs(screened=False)
    transports = {"anthropic": FakeTransport(batch_prefix="scope"), "openai": FakeTransport()}
    screen = transports["anthropic"]
    if failure == "upload":
        screen.upload_error = LLMError("no screening API access")
    elif failure == "create":
        screen.create_error = LLMError("screening request outcome unknown")
        screen.accept_before_error = True
    elif failure == "retrieve":

        def unavailable(batch_id):
            raise LLMError("screening service unavailable")

        screen.retrieve = unavailable
    coordinator = BatchAnalysisCoordinator(pipeline=pipeline())
    coordinator._transports = transports
    run_id = coordinator.start(ids)
    if failure not in {"upload", "create", "retrieve"}:
        custom_id = screen.requests("scope-1")[0]["custom_id"]
        screen.complete(
            "scope-1",
            invalid=(custom_id,) if failure == "invalid" else (),
            missing=(custom_id,) if failure == "missing" else (),
            failed=(custom_id,) if failure == "provider_error" else (),
        )
        coordinator.sync()
    assert transports["openai"].created == ["batch-1"]
    assert journal(run_id)[1][0].scope_data["status"] == "fail_open"
    transports["openai"].complete("batch-1")
    coordinator.sync()
    transports["openai"].complete("batch-2")
    coordinator.sync()
    assert journal(run_id)[0].state == "completed"
    assert screen.upload_attempts == 1


def test_batch_manual_override_keeps_decisively_outside_offer(isolated_db):
    job_id = seed_jobs(screened=False)[0]
    with session_scope() as session:
        job = session.get(Job, job_id)
        job.score.components = {
            **job.score.components,
            "manual_filter_bypass": {
                "schema_version": 1,
                "origin": "manual_addition",
                "added_at": "2026-10-09T00:00:00Z",
            },
        }
    screen, extract = FakeTransport(batch_prefix="scope"), FakeTransport()
    coordinator = BatchAnalysisCoordinator(pipeline=pipeline())
    coordinator._transports = {"anthropic": screen, "openai": extract}
    run_id = coordinator.start([job_id])
    record = screen.requests("scope-1")[0]
    screen.complete(
        "scope-1", answers={record["custom_id"]: verdict("clearly_outside", "sufficient")}
    )
    coordinator.sync()
    assert extract.created == ["batch-1"]
    assert journal(run_id)[1][0].state == "extraction_pending"
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.archived_at is None
        assert job.score.components["extraction_screen"]["decision"] == "evaluate_manual_override"
