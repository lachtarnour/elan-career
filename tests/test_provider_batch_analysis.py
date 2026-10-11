"""Both provider journals resume through native Claude mocks and isolated SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import LLMCache, LLMUsage
from smartapply.llm.anthropic_batch import AnthropicBatchTransport
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.usage import estimate_cost_usd
from smartapply.pipeline.batch_analysis import coordinator as coordinator_module
from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
from smartapply.pipeline.batch_analysis.presentation import analysis_workspace
from smartapply.pipeline.batch_analysis.results import schema_for_request
from tests.fit_fixtures import decision_response, evaluation_from_user
from tests.llm_config_helpers import change_call, configure_calls
from tests.test_batch_analysis import (
    FakeTransport,
    counts,
    extraction_offer,
    journal,
    pipeline,
    seed_jobs,
)

pytestmark = pytest.mark.usefixtures("isolated_db")


@pytest.fixture(autouse=True)
def batch_routes(monkeypatch, tmp_path):
    return configure_calls(monkeypatch, tmp_path, **{
        purpose: {
            "provider": "anthropic", "model": "claude-haiku-5-5", "reasoning": "high",
            "max_output_tokens": tokens,
        }
        for purpose, tokens in (
            ("job_scope_screening", 4096),
            ("job_offer_extraction", 4321),
            ("job_fit_assessment", 12000),
        )
    })


class NativeClaudeClient:
    """Message Batches API surface, keeping wire IDs and native results intact."""

    def __init__(self):
        self.messages = self.batches = self
        self.inputs = {}
        self.snapshots = {}
        self.outputs = {}
        self.created = []
        self.retrieved = []
        self.cancelled = []
        self.create_error = None

    def with_options(self, *, max_retries):
        assert max_retries == 0
        return self

    def create(self, *, requests):
        batch_id = f"msgbatch-{len(self.created) + 1}"
        self.created.append(batch_id)
        self.inputs[batch_id] = requests
        now = datetime.now(timezone.utc)
        value = {
            "id": batch_id, "processing_status": "in_progress",
            "request_counts": {"processing": len(requests), "succeeded": 0,
                               "errored": 0, "canceled": 0, "expired": 0},
            "created_at": now, "expires_at": now + timedelta(hours=24),
        }
        self.snapshots[batch_id] = value
        if self.create_error:
            raise self.create_error
        return value

    def retrieve(self, batch_id):
        self.retrieved.append(batch_id)
        return self.snapshots[batch_id]

    def list(self, *, limit):
        assert limit == 100
        return self.snapshots.values()

    def results(self, batch_id):
        return self.outputs[batch_id]

    def cancel(self, batch_id):
        self.cancelled.append(batch_id)
        self.snapshots[batch_id]["processing_status"] = "canceling"
        return self.snapshots[batch_id]

    def complete(self, batch_id, *, outcome="succeeded", answers=None, stop_reason="end_turn"):
        answers = answers or {}
        output = []
        for request in reversed(self.inputs[batch_id]):
            custom_id, params = request["custom_id"], request["params"]
            original_id = custom_id.partition("_")[2]
            if outcome == "succeeded":
                user = params["messages"][0]["content"]
                if isinstance(user, list):
                    user = "".join(block["text"] for block in user)
                answer = answers.get(original_id) or (
                    __import__("smartapply.llm.schemas", fromlist=["JobScopeScreening"]).JobScopeScreening.model_validate({"mission_screen": {"mission_relation": "unknown", "mission_evidence": "insufficient"}}) if original_id.startswith("screening-")
                    else extraction_offer() if original_id.startswith("extraction-")
                    else evaluation_from_user(user)
                )
                content = answer if isinstance(answer, str) else answer.model_dump_json()
                result = {"type": "succeeded", "message": {
                    "id": "msg-" + custom_id, "model": params["model"],
                    "stop_reason": stop_reason, "content": [{"type": "text", "text": content}],
                    "usage": {"input_tokens": 20, "output_tokens": 50,
                              "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10},
                }}
            else:
                result = {"type": outcome}
            output.append({"custom_id": custom_id, "result": result})
        self.outputs[batch_id] = output
        self.snapshots[batch_id].update(
            processing_status="ended", ended_at=datetime.now(timezone.utc),
            request_counts={"processing": 0, outcome: len(output)},
        )


def test_mixed_runs_resume_with_saved_provider_model_and_budget_after_settings_change(
    monkeypatch, batch_routes
):
    for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment"):
        change_call(batch_routes, purpose, provider="openai", model="gpt-5.4-mini",
                    reasoning="medium")
    ids = seed_jobs(2)
    openai = FakeTransport()
    native = NativeClaudeClient()
    anthropic = AnthropicBatchTransport(native)
    selected = []

    def factory(provider, *, settings):
        selected.append(provider)
        return {"openai": openai, "anthropic": anthropic}[provider]

    monkeypatch.setattr(coordinator_module, "get_batch_transport", factory)
    first = BatchAnalysisCoordinator(pipeline=pipeline()).start([ids[0]])
    for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment"):
        change_call(batch_routes, purpose, provider="anthropic", model="claude-haiku-5-5",
                    reasoning="high")
    second = BatchAnalysisCoordinator(pipeline=pipeline()).start([ids[1]])
    assert journal(first)[0].provider == "openai"
    assert journal(second)[0].provider == "anthropic"
    assert native.inputs["msgbatch-1"][0]["params"]["max_tokens"] == 4321
    # Every subsequent coordinator is constructed with a different selection.
    change_call(batch_routes, "job_offer_extraction", model="claude-sonnet-5-5")
    openai.complete("batch-1")
    native.complete("msgbatch-1")
    BatchAnalysisCoordinator().sync()
    assert len(openai.created) == 2 and len(native.created) == 2
    assert journal(second)[0].model == "claude-haiku-5-5"
    native.complete("msgbatch-2")
    openai.complete("batch-2")
    BatchAnalysisCoordinator().sync()
    assert journal(first)[0].state == journal(second)[0].state == "completed"
    assert counts()["analyses"] == 2 and counts()["paid"] == 4
    assert set(selected) == {"openai", "anthropic"}
    with session_scope() as session:
        assert {usage.provider for usage in session.scalars(select(LLMUsage))} == {
            "openai", "anthropic",
        }
        assert {cached.provider for cached in session.scalars(select(LLMCache))} == {
            "openai", "anthropic",
        }
        usage = session.scalar(select(LLMUsage).where(LLMUsage.provider == "anthropic"))
        assert usage.prompt_tokens == 110 and usage.cached_prompt_tokens == 80
        assert usage.cache_write_prompt_tokens == 10
        assert usage.cost_usd == pytest.approx(estimate_cost_usd(
            "claude-haiku-5-5", 110, 50,
            cached_prompt_tokens=80, cache_write_prompt_tokens=10,
        ) * 0.5)
    runs = {run["id"]: run for run in analysis_workspace()["runs"]}
    assert runs[second]["provider_label"] == "Anthropic"
    assert runs[second]["model"] == "claude-haiku-5-5"


def test_native_unknown_submission_waits_and_recovers_without_buying_a_second_extraction():
    ids = seed_jobs()
    native = NativeClaudeClient()
    native.create_error = TimeoutError("response lost after provider acceptance")
    transport = AnthropicBatchTransport(native)
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    assert journal(run_id)[0].state == "attention"
    assert native.created == ["msgbatch-1"]
    for _ in range(3):
        BatchAnalysisCoordinator(transport=transport).sync()
    assert native.created == ["msgbatch-1"]
    native.create_error = None
    native.complete("msgbatch-1")
    BatchAnalysisCoordinator(transport=transport).sync()
    assert native.created == ["msgbatch-1", "msgbatch-2"]
    assert all("_evaluation-" in request["custom_id"] for request in native.inputs["msgbatch-2"])
    native.complete("msgbatch-2")
    BatchAnalysisCoordinator(transport=transport).sync()
    assert journal(run_id)[0].state == "completed" and counts()["paid"] == 2


def test_native_ambiguous_screening_waits_for_recovery_without_second_post():
    ids = seed_jobs(screened=False)
    native = NativeClaudeClient()
    native.create_error = TimeoutError("screening response lost after acceptance")
    transport = AnthropicBatchTransport(native)
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    assert journal(run_id)[0].state == "attention"
    assert journal(run_id)[2][0].state == "submitting"
    for _ in range(3):
        BatchAnalysisCoordinator(transport=transport).sync()
    coordinator.retry(run_id)
    assert native.created == ["msgbatch-1"]
    assert journal(run_id)[1][0].state == "screening_pending"
    native.create_error = None
    native.complete("msgbatch-1")
    BatchAnalysisCoordinator(transport=transport).sync()
    assert native.created == ["msgbatch-1", "msgbatch-2"]
    assert all("_extraction-" in request["custom_id"] for request in native.inputs["msgbatch-2"])


def test_batch_usage_prices_served_snapshot_and_cache_keeps_requested_model(batch_routes):
    requested = "claude-sonnet-4-5"
    for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment"):
        change_call(batch_routes, purpose, model=requested, reasoning="auto")
    ids = seed_jobs()
    native = NativeClaudeClient()
    coordinator = BatchAnalysisCoordinator(
        transport=AnthropicBatchTransport(native), pipeline=pipeline()
    )
    served = "claude-sonnet-5-5-20261001"
    run_id = coordinator.start(ids)
    native.complete("msgbatch-1")
    native.outputs["msgbatch-1"][0]["result"]["message"]["model"] = served
    coordinator.sync()
    run, _, waves = journal(run_id)
    assert run.model == requested and run.fit_context["model"] == requested
    assert waves[0].requests[0]["body"]["model"] == requested
    with session_scope() as session:
        usage = session.scalar(select(LLMUsage).where(LLMUsage.cached.is_(False)))
        cached = session.scalar(select(LLMCache))
        assert usage.model == served and cached.model == requested
        expected = estimate_cost_usd(
            served, 110, 50, cached_prompt_tokens=80, cache_write_prompt_tokens=10,
        ) * 0.5
        assert usage.cost_usd == pytest.approx(expected)
        assert usage.cost_usd != pytest.approx(estimate_cost_usd(
            requested, 110, 50, cached_prompt_tokens=80, cache_write_prompt_tokens=10,
        ) * 0.5)
        assert cached.cache_key == waves[0].requests[0]["cache_key"]


def test_native_cancel_keeps_received_extraction_and_never_submits_fit():
    ids = seed_jobs()
    native = NativeClaudeClient()
    transport = AnthropicBatchTransport(native)
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    coordinator.cancel(run_id)
    assert native.cancelled == ["msgbatch-1"]
    native.complete("msgbatch-1")
    coordinator.sync()
    assert journal(run_id)[0].state == "cancelled"
    assert journal(run_id)[1][0].offer_data is not None
    assert counts()["analyses"] == 0 and len(native.created) == 1


def test_native_truncated_fit_keeps_score_original_output_and_exact_cache(monkeypatch):
    ids = seed_jobs(2, exact_copies=True)
    native = NativeClaudeClient()
    coordinator = BatchAnalysisCoordinator(
        transport=AnthropicBatchTransport(native), pipeline=pipeline()
    )
    first = coordinator.start([ids[0]])
    native.complete("msgbatch-1")
    coordinator.sync()
    fit_id = native.inputs["msgbatch-2"][0]["custom_id"].partition("_")[2]
    content = json.dumps(decision_response())[:-1] + ',"neutral_offer_re'
    native.complete("msgbatch-2", answers={fit_id: content}, stop_reason="max_tokens")
    coordinator.sync()
    assert journal(first)[0].state == "completed"
    with session_scope() as session:
        from smartapply.database.models import Job

        analysis = session.get(Job, ids[0]).analysis
        assert analysis.fit_score == 0.95
        assert analysis.fit_diagnostics["original_response"] == content
        assert analysis.fit_diagnostics["diagnostic_checks"]["warnings"]
        assert session.scalar(select(LLMCache).where(LLMCache.response == content)) is not None
    # Another exact copy uses both cached results, including the recoverable
    # fit response. Its annotations cannot trigger another provider call.
    second = coordinator.start([ids[1]])
    assert journal(second)[0].state == "completed"
    assert native.created == ["msgbatch-1", "msgbatch-2"]
    assert counts()["analyses"] == 2 and counts()["cached"] == 2
    immediate = AnthropicProvider(model="claude-haiku-5-5")

    def unexpected(body):
        raise AssertionError("Batch cache must prevent another immediate call")

    monkeypatch.setattr(immediate, "_generate", unexpected)
    body = journal(first)[2][1].requests[0]["body"]
    parsed = immediate.complete_json(
        system=body["messages"][0]["content"], user=body["messages"][1]["content"],
        schema=schema_for_request(body), model=body["model"], purpose="job_fit_assessment",
        effort="high", max_completion_tokens=body["max_completion_tokens"],
    )
    assert parsed.original_text == content and parsed.levels == (4, 4, 3)
    assert native.created == ["msgbatch-1", "msgbatch-2"]
