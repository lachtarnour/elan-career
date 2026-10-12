"""Restart a selected offer through Create without buying its acquired calls again."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from smartapply.concurrency import generation_owner, job_operation
from smartapply.config import get_llm_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.generation_runs import GenerationCall, GenerationItem, GenerationRun
from smartapply.database.models import Application
from smartapply.llm import DocumentTextReview, MockLLMProvider
from smartapply.llm.provider import LLMValidationError
from smartapply.pipeline import Pipeline
from smartapply.pipeline.generation.coordinator import GenerationCoordinator
from smartapply.pipeline.generation.presentation import generation_workspace
from smartapply.pipeline.generation.replay import PURPOSES
from smartapply.profile import get_profile
from tests import test_generation_batch_pipeline as batch_tests
from tests.llm_config_helpers import configure_calls
from tests.test_generation_batch_pipeline import (
    Harness,
    journal,
    native_routes,
)
from tests.test_generation_pipeline import CountingProvider

STAGES = ("cv_adaptation", "application_texts", "document_review")


@pytest.fixture
def analyzed_jobs(isolated_db):
    return batch_tests.analyzed_jobs.__wrapped__(isolated_db)


def _calls_for_offer(job_id):
    with session_scope() as session:
        return list(session.scalars(select(GenerationCall).join(GenerationItem).where(
            GenerationItem.job_id == job_id,
        ).order_by(GenerationCall.id)))


def _current_item(run_id, job_id):
    with session_scope() as session:
        return session.scalar(select(GenerationItem).where(
            GenerationItem.run_id == run_id, GenerationItem.job_id == job_id,
        ))


def _pause_batch(harness, ids, stage):
    coordinator = harness.coordinator()
    run_id = coordinator.start(ids, mode="batch")
    for _ in range(STAGES.index(stage)):
        harness.complete(run_id)
        coordinator.sync()
    coordinator.cancel(run_id)
    harness.complete(run_id, status="cancelled")
    coordinator.sync()
    assert journal(run_id)[0].state == "paused"
    return run_id


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("next_mode", ["immediate", "batch"])
def test_immediate_create_again_reuses_offer_calls_in_either_mode(
    analyzed_jobs, monkeypatch, tmp_path, stage, next_mode,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    first_provider = CountingProvider(stop_after=stage)
    first_run = GenerationCoordinator(provider=first_provider).start(
        [job_id], mode="immediate", stop_requested=lambda: first_provider.stopped,
    )
    acquired = STAGES[:STAGES.index(stage) + 1]
    assert generation_workspace()["resumable_job_ids"] == [job_id]
    application_id = _current_item(first_run, job_id).application_id
    acquired_calls = {call.id: (call.request_key, call.response_data)
                      for call in _calls_for_offer(job_id)}
    second_provider = CountingProvider()
    harness = Harness(monkeypatch)
    next_run = GenerationCoordinator(provider=second_provider).start([job_id], mode=next_mode)
    if next_mode == "batch":
        harness.finish(next_run)
    with session_scope() as session:
        assert session.get(GenerationRun, next_run).state == "completed"
        application = session.get(Application, application_id)
        assert application.cv_json and application.documents
    assert generation_workspace()["resumable_job_ids"] == []
    assert _current_item(next_run, job_id).application_id == application_id
    assert all(first_provider.calls[purpose] == 1 for purpose in acquired)
    assert all(second_provider.calls[purpose] == 0 for purpose in acquired)
    assert all(harness.submitted[purpose] == 0 for purpose in acquired)
    total = first_provider.calls + second_provider.calls + harness.submitted
    assert total == Counter({purpose: 1 for purpose in STAGES})
    assert {call.id: (call.request_key, call.response_data)
            for call in _calls_for_offer(job_id) if call.id in acquired_calls} == acquired_calls


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("next_mode", ["immediate", "batch"])
def test_batch_create_again_reuses_offer_calls_in_either_mode(
    analyzed_jobs, monkeypatch, tmp_path, stage, next_mode,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    first_run = _pause_batch(harness, [job_id], stage)
    acquired = STAGES[:STAGES.index(stage) + 1]
    application_id = _current_item(first_run, job_id).application_id
    provider = CountingProvider()
    next_run = GenerationCoordinator(provider=provider).start([job_id], mode=next_mode)
    if next_mode == "batch":
        harness.finish(next_run)
    with session_scope() as session:
        assert session.get(GenerationRun, next_run).state == "completed"
        assert session.get(Application, application_id).cv_json
    assert _current_item(next_run, job_id).application_id == application_id
    assert all(provider.calls[purpose] == 0 for purpose in acquired)
    assert harness.submitted + provider.calls == Counter({purpose: 1 for purpose in STAGES})


def test_restarting_one_offer_does_not_consume_or_restart_the_other_paused_offer(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs(2)
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    first_run = _pause_batch(harness, ids, "cv_adaptation")
    application_ids = {item.job_id: item.application_id for item in journal(first_run)[1]}
    second_saved = deepcopy(_calls_for_offer(ids[1])[0].response_data)
    provider = CountingProvider()
    first_selected = GenerationCoordinator(provider=provider).start([ids[0]], mode="immediate")
    assert _current_item(first_selected, ids[0]).application_id == application_ids[ids[0]]
    assert provider.calls == Counter(application_texts=1, document_review=1)
    with session_scope() as session:
        assert session.get(Application, application_ids[ids[0]]).cv_json
        assert not session.get(Application, application_ids[ids[1]]).cv_json
    assert _calls_for_offer(ids[1])[0].response_data == second_saved
    second_selected = GenerationCoordinator(provider=provider).start([ids[1]], mode="immediate")
    assert _current_item(second_selected, ids[1]).application_id == application_ids[ids[1]]
    assert provider.calls == Counter(application_texts=2, document_review=2)
    assert harness.submitted == Counter(cv_adaptation=2)


def test_offer_restart_preserves_frozen_profile_routes_and_date(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    provider = CountingProvider(stop_after="application_texts")
    first_run = GenerationCoordinator(provider=provider).start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    old_run = journal(first_run)[0]
    frozen_profile = deepcopy(old_run.profile_data)
    frozen_routes = deepcopy(old_run.routes)
    frozen_date = old_run.reference_date
    settings = get_llm_settings()
    for route in settings.calls.values():
        route["model"] = "gpt-5.4"
        route["max_output_tokens"] += 1000
    monkeypatch.setattr("smartapply.pipeline.generation.coordinator.get_llm_settings", lambda: settings)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    monkeypatch.setattr("smartapply.pipeline.generation.coordinator._now", lambda: tomorrow)
    changed_profile = get_profile().model_copy(deep=True)
    changed_profile.identity.full_name = "Changed profile should not replace frozen candidate"
    captures = []

    def factory(**kwargs):
        captures.append({
            "profile": kwargs["profile"].model_dump(mode="json"),
            "routes": deepcopy(kwargs["llm"].routes),
            "date": kwargs["llm"].reference_date.isoformat(),
        })
        return Pipeline(**kwargs)

    provider.stopped, provider.stop_after = False, None
    next_run = GenerationCoordinator(
        provider=provider, profile=changed_profile, pipeline_factory=factory,
    ).start([job_id], mode="immediate")
    assert captures == [{"profile": frozen_profile, "routes": frozen_routes, "date": frozen_date}]
    assert journal(next_run)[0].state == "completed"
    assert provider.calls == Counter({purpose: 1 for purpose in STAGES})


def test_combining_offers_from_different_runs_preserves_each_frozen_context(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs(2)
    native_routes(monkeypatch, tmp_path)
    provider = CountingProvider(stop_after="cv_adaptation")
    first_date = datetime(2026, 10, 11, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("smartapply.pipeline.generation.coordinator._now", lambda: first_date)
    first_run = GenerationCoordinator(provider=provider).start(
        [ids[0]], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    original_context = journal(first_run)[0]
    settings = get_llm_settings()
    for route in settings.calls.values():
        route["max_output_tokens"] += 500
    monkeypatch.setattr("smartapply.pipeline.generation.coordinator.get_llm_settings", lambda: settings)
    monkeypatch.setattr(
        "smartapply.pipeline.generation.coordinator._now", lambda: first_date + timedelta(days=1),
    )
    changed_profile = get_profile().model_copy(deep=True)
    changed_profile.identity.full_name = "Other frozen profile"
    provider.stopped = False
    second_run = GenerationCoordinator(provider=provider, profile=changed_profile).start(
        [ids[1]], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    second_context = journal(second_run)[0]
    expected = [{"profile": saved.profile_data, "routes": saved.routes, "date": saved.reference_date}
                for saved in (original_context, second_context)]
    captures = []

    def factory(**kwargs):
        captures.append({
            "profile": kwargs["profile"].model_dump(mode="json"),
            "routes": deepcopy(kwargs["llm"].routes),
            "date": kwargs["llm"].reference_date.isoformat(),
        })
        return Pipeline(**kwargs)

    fresh_provider = CountingProvider()
    combined_run = GenerationCoordinator(provider=fresh_provider, pipeline_factory=factory).start(
        ids, mode="immediate",
    )
    assert journal(combined_run)[0].state == "completed"
    assert captures == expected
    assert provider.calls == Counter(cv_adaptation=2)
    assert fresh_provider.calls == Counter(application_texts=2, document_review=2)


def test_failed_offer_relaunch_retries_only_failed_stage(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)

    class FailLetterOnce(CountingProvider):
        def complete_json(self, **kwargs):
            if kwargs["purpose"] == "application_texts" and not self.calls["application_texts"]:
                self.calls["application_texts"] += 1
                raise LLMValidationError("Known invalid response")
            return super().complete_json(**kwargs)

    provider = FailLetterOnce()
    first_run = GenerationCoordinator(provider=provider).start([job_id], mode="immediate")
    assert journal(first_run)[0].state == "failed"
    assert provider.calls == Counter(cv_adaptation=1, application_texts=1)
    harness = Harness(monkeypatch)
    next_run = GenerationCoordinator(provider=provider).start([job_id], mode="batch")
    harness.finish(next_run)
    assert provider.calls == Counter(cv_adaptation=1, application_texts=1)
    assert harness.submitted == Counter(application_texts=1, document_review=1)


def test_unknown_immediate_offer_cannot_be_resent_by_starting_batch(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)

    class LostReply(CountingProvider):
        def complete_json(self, **kwargs):
            self.calls[kwargs["purpose"]] += 1
            raise TimeoutError("Reply lost after a possible charge")

    provider = LostReply()
    first_run = GenerationCoordinator(provider=provider).start([job_id], mode="immediate")
    assert journal(first_run)[0].state == "attention"
    harness = Harness(monkeypatch)
    with pytest.raises(ValueError, match="vérifier|envoi|cours"):
        harness.coordinator().start([job_id], mode="batch")
    assert generation_workspace()["resumable_job_ids"] == []
    assert provider.calls == Counter(cv_adaptation=1)
    assert not harness.openai.created
    assert _calls_for_offer(job_id)[0].state == "uncertain"


def test_outstanding_batch_offer_cannot_be_resent_by_starting_immediate(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    run_id = harness.coordinator().start([job_id], mode="batch")
    assert generation_workspace()["resumable_job_ids"] == []
    provider = CountingProvider()
    with pytest.raises((ValueError, RuntimeError)):
        GenerationCoordinator(provider=provider).start([job_id], mode="immediate")
    assert not provider.calls
    assert harness.openai.created == ["batch-1"]
    assert journal(run_id)[2][0].state == "submitted"


def test_abandoned_offer_new_create_reuses_paid_response(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    provider = CountingProvider(stop_after="cv_adaptation")
    coordinator = GenerationCoordinator(provider=provider)
    first_run = coordinator.start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    with write_session_scope() as session:
        application_id = _current_item(first_run, job_id).application_id
        session.get(Application, application_id).notes = "Keep this candidate note"
    coordinator.abandon(first_run)
    harness = Harness(monkeypatch)
    next_run = harness.coordinator().start([job_id], mode="batch")
    harness.finish(next_run)
    assert provider.calls == Counter(cv_adaptation=1)
    assert harness.submitted == Counter(application_texts=1, document_review=1)
    with session_scope() as session:
        application = session.get(Application, _current_item(next_run, job_id).application_id)
        assert application.cv_json and application.notes == "Keep this candidate note"


def test_offer_restart_reuses_selection_correction_and_runs_python_assembly(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    valid_selection = MockLLMProvider._default_registry["cv_adaptation"]
    MockLLMProvider.register("cv_adaptation", valid_selection.model_copy(update={
        "selected_project_ids": ["proj_evidence_rag"],
    }))
    MockLLMProvider.register("cv_selection_correction", valid_selection)
    provider = CountingProvider(stop_after="cv_selection_correction")
    first_run = GenerationCoordinator(provider=provider).start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    assert journal(first_run)[0].state == "paused"
    assert provider.calls == Counter(cv_adaptation=1, cv_selection_correction=1)
    harness = Harness(monkeypatch)
    next_run = harness.coordinator().start([job_id], mode="batch")
    harness.finish(next_run)
    assert harness.submitted == Counter(application_texts=1, document_review=1)
    with session_scope() as session:
        application = session.get(Application, _current_item(next_run, job_id).application_id)
        assert application.cv_json["selected_project_ids"] == valid_selection.selected_project_ids
        assert application.cv_json["selected_experiences"]


def test_offer_restart_replays_document_correction_then_requests_confirmation_only(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    selection = MockLLMProvider._default_registry["cv_adaptation"]
    MockLLMProvider.register("cv_adaptation", selection.model_copy(update={
        "cv_title": "Principal " + selection.cv_title,
    }))
    finding = {
        "field": "cv_title", "passage": "Principal ",
        "reason": "The supplied candidate evidence does not support this seniority.",
        "source_ids": ["candidate:provided_evidence"], "category": "factual",
        "replacement": None,
    }
    MockLLMProvider.register("document_review", DocumentTextReview(issues=[finding]))
    MockLLMProvider.register("document_correction", DocumentTextReview(issues=[{
        **finding, "replacement": "",
    }]))
    provider = CountingProvider(stop_after="document_correction")
    first_run = GenerationCoordinator(provider=provider).start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    assert journal(first_run)[0].state == "paused"
    assert provider.calls == Counter(
        cv_adaptation=1, application_texts=1, document_review=1, document_correction=1,
    )
    MockLLMProvider.register("document_review", DocumentTextReview(issues=[]))
    harness = Harness(monkeypatch)
    next_run = harness.coordinator().start([job_id], mode="batch")
    harness.finish(next_run)
    assert harness.submitted == Counter(document_review=1)
    current = _current_item(next_run, job_id)
    assert [entry["decision"] for entry in current.result["document_review"]] == [
        "correction_requested", "revision_proposed", "accepted",
    ]
    with session_scope() as session:
        assert not session.get(Application, current.application_id).cv_json["cv_title"].startswith("Principal ")


def _mock_routes(monkeypatch, tmp_path):
    configure_calls(monkeypatch, tmp_path, **{
        purpose: {
            "provider": "mock", "model": "mock-smart", "reasoning": "auto",
            "max_output_tokens": 6000,
        }
        for purpose in PURPOSES
    })


def test_batch_resume_uses_native_frozen_routes_when_current_routes_are_mock(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    provider = CountingProvider(stop_after="cv_adaptation")
    first_run = GenerationCoordinator(provider=provider).start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    _mock_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    next_run = harness.coordinator().start([job_id], mode="batch")
    harness.finish(next_run)
    assert journal(first_run)[1][0].state == "transferred"
    assert journal(next_run)[0].state == "completed"
    assert provider.calls == Counter(cv_adaptation=1)
    assert harness.submitted == Counter(application_texts=1, document_review=1)
    assert all(call.provider == "openai" for call in journal(next_run)[2])


def test_batch_rejects_mock_frozen_routes_without_transferring_or_buying_calls(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    _mock_routes(monkeypatch, tmp_path)
    provider = CountingProvider(stop_after="cv_adaptation")
    first_run = GenerationCoordinator(provider=provider).start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    original_item = _current_item(first_run, job_id)
    acquired_calls = {call.id: (call.request_key, call.response_data)
                      for call in _calls_for_offer(job_id)}
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    with pytest.raises(ValueError, match="Batch|OpenAI|Anthropic"):
        harness.coordinator().start([job_id], mode="batch")
    with session_scope() as session:
        assert list(session.scalars(select(GenerationRun.id))) == [first_run]
    assert _current_item(first_run, job_id).state == original_item.state
    assert _current_item(first_run, job_id).application_id == original_item.application_id
    assert {call.id: (call.request_key, call.response_data)
            for call in _calls_for_offer(job_id)} == acquired_calls
    assert not harness.openai.created
    assert provider.calls == Counter(cv_adaptation=1)
    provider.stopped, provider.stop_after = False, None
    next_run = GenerationCoordinator(provider=provider).start([job_id], mode="immediate")
    assert journal(next_run)[0].state == "completed"
    assert provider.calls == Counter({purpose: 1 for purpose in STAGES})
    assert all(call.provider == "mock" for call in journal(next_run)[2])


def test_resumable_offers_exclude_a_current_local_operation(analyzed_jobs):
    job_id = analyzed_jobs()[0]
    provider = CountingProvider(stop_after="cv_adaptation")
    run_id = GenerationCoordinator(provider=provider).start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    assert generation_workspace()["resumable_job_ids"] == [job_id]
    with generation_owner(run_id), job_operation([job_id], operation="current generation worker"):
        assert generation_workspace()["resumable_job_ids"] == []
    assert generation_workspace()["resumable_job_ids"] == [job_id]


def test_resumable_offers_exclude_an_analysis_claim_until_released(analyzed_jobs):
    job_id = analyzed_jobs()[0]
    provider = CountingProvider(stop_after="cv_adaptation")
    run_id = GenerationCoordinator(provider=provider).start(
        [job_id], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    assert generation_workspace()["resumable_job_ids"] == [job_id]
    source = _current_item(run_id, job_id)
    with write_session_scope() as session:
        session.add(AnalysisBatchRun(
            id="analysis-claim", state="active", provider="openai", model="gpt-5.4",
            profile_data={},
        ))
        session.flush()
        session.add(AnalysisBatchItem(
            run_id="analysis-claim", job_id=job_id, job_snapshot=source.job_snapshot,
            title=source.title, company=source.company, analyzer_input={},
        ))
    assert generation_workspace()["resumable_job_ids"] == []
    with write_session_scope() as session:
        session.get(AnalysisBatchRun, "analysis-claim").state = "cancelled"
    assert generation_workspace()["resumable_job_ids"] == [job_id]


def test_three_cross_mode_offer_restarts_buy_each_call_once_and_finish_locally(
    analyzed_jobs, monkeypatch, tmp_path,
):
    job_id = analyzed_jobs()[0]
    native_routes(monkeypatch, tmp_path)
    captures = []

    def factory(**kwargs):
        captures.append({
            "profile": kwargs["profile"].model_dump(mode="json"),
            "routes": deepcopy(kwargs["llm"].routes),
            "date": kwargs["llm"].reference_date.isoformat(),
        })
        return Pipeline(**kwargs)

    first_provider = CountingProvider(stop_after="cv_adaptation")
    first_run = GenerationCoordinator(provider=first_provider, pipeline_factory=factory).start(
        [job_id], mode="immediate", stop_requested=lambda: first_provider.stopped,
    )
    original = journal(first_run)[0]
    expected_context = {
        "profile": deepcopy(original.profile_data), "routes": deepcopy(original.routes),
        "date": original.reference_date,
    }
    application_id = _current_item(first_run, job_id).application_id
    changed_profile = get_profile().model_copy(deep=True)
    changed_profile.identity.full_name = "Updated candidate profile"
    settings = get_llm_settings()
    for route in settings.calls.values():
        route["max_output_tokens"] += 1000
    monkeypatch.setattr("smartapply.pipeline.generation.coordinator.get_llm_settings", lambda: settings)
    later_date = datetime.now(timezone.utc) + timedelta(days=3)
    monkeypatch.setattr("smartapply.pipeline.generation.coordinator._now", lambda: later_date)

    harness = Harness(monkeypatch)
    second_coordinator = GenerationCoordinator(
        provider=harness.provider, profile=changed_profile, pipeline_factory=factory,
    )
    second_run = second_coordinator.start([job_id], mode="batch")
    assert [call.purpose for call in journal(second_run)[2]] == ["cv_adaptation", "application_texts"]
    second_coordinator.cancel(second_run)
    harness.complete(second_run, status="cancelled")
    second_coordinator.sync()
    assert journal(second_run)[0].state == "paused"

    third_provider = CountingProvider(stop_after="document_review")
    third_run = GenerationCoordinator(
        provider=third_provider, profile=changed_profile, pipeline_factory=factory,
    ).start([job_id], mode="immediate", stop_requested=lambda: third_provider.stopped)
    assert journal(third_run)[0].state == "paused"
    assert third_provider.calls == Counter(document_review=1)
    with session_scope() as session:
        assert not session.get(Application, application_id).cv_json

    native_requests_before = list(harness.openai.created)
    fourth_provider = CountingProvider()
    fourth_run = GenerationCoordinator(
        provider=fourth_provider, profile=changed_profile, pipeline_factory=factory,
    ).start([job_id], mode="batch")
    assert journal(fourth_run)[0].state == "completed"
    assert not journal(fourth_run)[3]
    assert not fourth_provider.calls
    assert harness.openai.created == native_requests_before
    assert all(capture == expected_context for capture in captures)
    assert len(captures) >= 4
    assert first_provider.calls + harness.submitted + third_provider.calls == Counter({
        purpose: 1 for purpose in STAGES
    })
    for run_id in (first_run, second_run, third_run):
        assert _current_item(run_id, job_id).state == "transferred"
    assert _current_item(fourth_run, job_id).application_id == application_id
    with session_scope() as session:
        application = session.get(Application, application_id)
        assert application.cv_json and application.documents
