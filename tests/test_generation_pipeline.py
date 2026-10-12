"""Exercise replay using the real CV assembly, review and publication path."""

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from smartapply.config import get_llm_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.generation_runs import GenerationCall, GenerationItem, GenerationRun
from smartapply.database.models import Application, Job
from smartapply.llm import MockLLMProvider
from smartapply.offers import ManualOfferInput
from smartapply.pipeline import Pipeline
from smartapply.pipeline.generation.coordinator import GenerationCoordinator
from smartapply.pipeline.generation.presentation import generation_workspace
from tests.test_pipeline import _register_llm_responses


@pytest.fixture
def analyzed(isolated_db):
    _register_llm_responses()
    pipeline = Pipeline()
    ingest = pipeline.ingest_manual_offer(ManualOfferInput(
        title="Data Scientist NLP", company="Acme AI", location="Paris",
        description="Build RAG pipelines and productionize ML services with Python and PyTorch.",
        application_url="https://acme.example/job",
    ))
    job_id = ingest.job_ids[0]
    pipeline.prepare_analysis(job_ids=[job_id], exclude_applications=False)
    assert pipeline.analyze_jobs([job_id], exclude_applications=False).analyzed == 1
    return job_id


class CountingProvider(MockLLMProvider):
    def __init__(self, stop_after=None):
        super().__init__()
        self.calls = Counter()
        self.stop_after = stop_after
        self.stopped = False

    def complete_json(self, **kwargs):
        self.calls[kwargs["purpose"]] += 1
        result = super().complete_json(**kwargs)
        if kwargs["purpose"] == self.stop_after:
            self.stopped = True
        return result


def test_immediate_production_pipeline_publishes_atomic_journal(analyzed):
    provider = CountingProvider()
    coordinator = GenerationCoordinator(provider=provider)
    run_id = coordinator.start([analyzed], mode="immediate")
    with session_scope() as session:
        assert session.get(GenerationRun, run_id).state == "completed"
        item = session.scalar(select(GenerationItem))
        assert item.state == "completed"
        assert item.result["application_id"] == item.application_id
        app = session.get(Application, item.application_id)
        assert app.cv_json and app.documents
        assert session.scalar(select(GenerationCall).where(GenerationCall.state != "completed")) is None
    assert provider.calls["cv_adaptation"] == 1
    assert provider.calls["application_texts"] == 1
    workspace = generation_workspace()
    assert workspace["runs"][0]["generated"] == 1
    assert workspace["runs"][0]["phase"] == "done"


def test_cleanup_error_cannot_demote_already_published_dossier(analyzed, monkeypatch):
    def failed_cleanup(self):
        raise OSError("simulated backup cleanup error")

    monkeypatch.setattr("smartapply.pipeline.output_paths.AtomicApplicationOutput.commit", failed_cleanup)
    run_id = GenerationCoordinator(provider=CountingProvider()).start([analyzed], mode="immediate")
    with session_scope() as session:
        assert session.get(GenerationRun, run_id).state == "completed"
        item = session.scalar(select(GenerationItem))
        assert item.state == "completed"
        assert session.get(Application, item.application_id).cv_json
    assert not generation_workspace()["runs"][0]["can_retry"]


@pytest.mark.parametrize("stage", ["cv_adaptation", "application_texts", "document_review"])
def test_stop_saves_paid_response_and_resume_only_next_call(analyzed, stage):
    provider = CountingProvider(stop_after=stage)
    coordinator = GenerationCoordinator(provider=provider)
    run_id = coordinator.start([analyzed], mode="immediate", stop_requested=lambda: provider.stopped)
    with session_scope() as session:
        run = session.get(GenerationRun, run_id)
        assert run.state == "paused"
        item = session.scalar(select(GenerationItem))
        app_id = item.application_id
        assert app_id is not None
        assert not session.get(Application, app_id).cv_json
        assert session.scalar(select(GenerationCall).where(GenerationCall.purpose == stage)).state == "completed"
    provider.stopped = False
    provider.stop_after = None
    # A new coordinator simulates a process restart; response cache is irrelevant.
    GenerationCoordinator(provider=provider).retry(run_id)
    with session_scope() as session:
        assert session.get(GenerationRun, run_id).state == "completed"
        assert session.scalar(select(GenerationItem)).application_id == app_id
    assert all(count == 1 for count in provider.calls.values())


def test_changed_offer_stops_without_new_calls(analyzed):
    provider = CountingProvider(stop_after="application_texts")
    coordinator = GenerationCoordinator(provider=provider)
    run_id = coordinator.start([analyzed], mode="immediate", stop_requested=lambda: provider.stopped)
    counts = provider.calls.copy()
    with write_session_scope() as session:
        session.get(Job, analyzed).description += " changed"
    provider.stopped = False
    GenerationCoordinator(provider=provider).retry(run_id)
    assert provider.calls == counts
    with session_scope() as session:
        assert session.scalar(select(GenerationItem)).state == "stale"
        assert session.scalar(select(Application)) is None


def test_durable_claim_blocks_mutation_but_create_reuses_the_offer_checkpoint(analyzed):
    from smartapply.concurrency import TaskConflictError, busy_job_ids, job_operation

    provider = CountingProvider(stop_after="cv_adaptation")
    coordinator = GenerationCoordinator(provider=provider)
    coordinator.start([analyzed], mode="immediate", stop_requested=lambda: provider.stopped)
    assert analyzed in busy_job_ids()
    with pytest.raises(TaskConflictError), job_operation([analyzed], operation="archivage"):
        pass
    provider.stopped, provider.stop_after = False, None
    restarted = coordinator.start([analyzed], mode="immediate")
    with session_scope() as session:
        assert session.get(GenerationRun, restarted).state == "completed"
    assert provider.calls["cv_adaptation"] == 1


def test_old_paused_reservation_is_available_for_shortlist_resume(analyzed):
    from smartapply.desktop.services import DesktopService

    with write_session_scope() as session:
        session.get(Job, analyzed).shortlisted_at = datetime.now(timezone.utc)
    provider = CountingProvider(stop_after="cv_adaptation")
    GenerationCoordinator(provider=provider).start(
        [analyzed], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    with write_session_scope() as session:
        session.scalar(select(Application)).updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    service = DesktopService()
    assert service.shortlist_summary().ready_to_generate == 1
    assert service.shortlisted_generation_ids() == [analyzed]


def test_interrupted_immediate_unknown_call_never_resubmits(analyzed):
    provider = CountingProvider(stop_after="cv_adaptation")
    coordinator = GenerationCoordinator(provider=provider)
    run_id = coordinator.start([analyzed], mode="immediate", stop_requested=lambda: provider.stopped)
    with write_session_scope() as session:
        call = session.scalar(select(GenerationCall))
        call.state, call.response_data = "submitting", None
        session.get(GenerationRun, run_id).state = "active"
    coordinator.sync()
    with session_scope() as session:
        assert session.get(GenerationRun, run_id).state == "attention"
    before = provider.calls.copy()
    with pytest.raises(ValueError, match="vérifier"):
        coordinator.retry(run_id)
    assert provider.calls == before


def test_frozen_reference_date_and_routes_on_resume(analyzed, monkeypatch):
    provider = CountingProvider(stop_after="cv_adaptation")
    run_id = GenerationCoordinator(provider=provider).start(
        [analyzed], mode="immediate", stop_requested=lambda: provider.stopped,
    )
    settings = get_llm_settings()
    settings.calls["cv_adaptation"]["max_output_tokens"] += 1000
    monkeypatch.setattr("smartapply.pipeline.generation.coordinator.get_llm_settings", lambda: settings)
    provider.stopped = False
    provider.stop_after = None
    GenerationCoordinator(provider=provider).retry(run_id)
    with session_scope() as session:
        assert session.get(GenerationRun, run_id).state == "completed"
    assert provider.calls["cv_adaptation"] == 1
