"""Durable replay identity, native inference retries and publication rollback."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import create_model
from sqlalchemy import select

from smartapply.concurrency import busy_job_ids, job_operation
from smartapply.config import get_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.generation_runs import GenerationCall, GenerationItem, GenerationRun
from smartapply.database.models import Application, Job
from smartapply.jobsearch.offer_state import reservation_is_stale
from smartapply.llm import MockLLMProvider
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.openai_provider import OpenAIProvider
from smartapply.llm.schemas import DocumentTextReview
from smartapply.offers import ManualOfferInput
from smartapply.pipeline import Pipeline
from smartapply.pipeline.applier import Applier
from smartapply.pipeline.errors import StaleTaskResultError
from smartapply.pipeline.generation.coordinator import GenerationCoordinator
from smartapply.pipeline.generation.replay import (
    GenerationCallFailed,
    ReplayProvider,
    frozen_settings,
)
from tests.test_pipeline import _register_llm_responses

pytestmark = pytest.mark.usefixtures("isolated_db")


def _seed():
    with write_session_scope() as session:
        job = Job(
            external_id="replay-test",
            title="AI Engineer",
            source_company="Acme",
            source="manual",
            description="Build ML services",
        )
        session.add(job)
        session.add(
            GenerationRun(
                id="replay-run",
                mode="immediate",
                profile_data={},
                routes={},
                reference_date="2026-10-11",
            )
        )
        session.flush()
        item = GenerationItem(
            run_id="replay-run",
            job_id=job.id,
            job_snapshot="snapshot",
            analysis_snapshot="analysis",
            title=job.title,
            company=job.source_company,
        )
        session.add(item)
        session.flush()
        return item.id


def _routes():
    return {
        "document_review": {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "reasoning": "medium",
            "max_output_tokens": 4096,
        }
    }


class _AnswerProvider:
    def __init__(self):
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        return DocumentTextReview(issues=[])


class _CountingProvider(MockLLMProvider):
    def __init__(self, *, stop_after=None):
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


def _analyzed_job():
    _register_llm_responses()
    pipeline = Pipeline()
    ingest = pipeline.ingest_manual_offer(
        ManualOfferInput(
            title="Data Scientist NLP",
            company="Acme AI",
            location="Paris",
            description="Build RAG pipelines and productionize ML services with Python and PyTorch.",
            application_url="https://acme.example/job",
        )
    )
    job_id = ingest.job_ids[0]
    pipeline.prepare_analysis(job_ids=[job_id], exclude_applications=False)
    assert pipeline.analyze_jobs([job_id], exclude_applications=False).analyzed == 1
    return job_id


def _replay(item_id, provider, *, routes=None, validate=None):
    return ReplayProvider(
        item_id=item_id,
        mode="immediate",
        routes=routes or _routes(),
        reference_date="2026-10-11",
        provider=provider,
        validate=validate or (lambda: None),
    )


def _kwargs():
    return {
        "system": "Check the text against its evidence.",
        "user": "Text and evidence",
        "schema": DocumentTextReview,
        "purpose": "document_review",
        "use_cache": False,
    }


@pytest.mark.parametrize("change", ["budget", "model", "effort", "provider", "schema", "prompt"])
def test_full_request_identity_prevents_reusing_an_incompatible_response(change):
    item_id = _seed()
    provider = _AnswerProvider()
    assert _replay(item_id, provider).complete_json(**_kwargs()).issues == []
    routes, kwargs = deepcopy(_routes()), _kwargs()
    route = routes["document_review"]
    if change == "budget":
        route["max_output_tokens"] += 1
    elif change == "model":
        route["model"] = "gpt-5.4"
    elif change == "effort":
        route["reasoning"] = "high"
    elif change == "provider":
        route.update(provider="anthropic", model="claude-haiku-5-5", reasoning="high")
    elif change == "schema":
        # A schema can change while its public class name remains unchanged.
        kwargs["schema"] = create_model("DocumentTextReview", revised_contract=(str, ...))
    else:
        kwargs["user"] += " changed"
    with pytest.raises(StaleTaskResultError):
        _replay(item_id, provider, routes=routes).complete_json(**kwargs)
    assert provider.calls == 1
    with session_scope() as session:
        calls = list(session.scalars(select(GenerationCall)))
        assert len(calls) == 1 and calls[0].state == "completed"


def test_changed_stored_request_body_cannot_replay_a_response():
    item_id = _seed()
    provider = _AnswerProvider()
    _replay(item_id, provider).complete_json(**_kwargs())
    with write_session_scope() as session:
        call = session.scalar(select(GenerationCall))
        changed = deepcopy(call.request_body)
        changed["messages"][1]["content"] = "An unrelated offer"
        call.request_body = changed
    with pytest.raises(StaleTaskResultError):
        _replay(item_id, provider).complete_json(**_kwargs())
    assert provider.calls == 1


def test_changed_offer_validation_prevents_replaying_completed_calls():
    item_id = _seed()
    provider = _AnswerProvider()
    _replay(item_id, provider).complete_json(**_kwargs())

    def stale():
        raise StaleTaskResultError()

    with pytest.raises(StaleTaskResultError):
        _replay(item_id, provider, validate=stale).complete_json(**_kwargs())
    assert provider.calls == 1


@pytest.mark.parametrize("native", ["openai", "anthropic"])
def test_native_immediate_connection_failure_is_never_automatically_resent(native):
    item_id = _seed()
    routes = _routes()
    if native == "anthropic":
        from anthropic import APIConnectionError

        routes["document_review"].update(
            provider="anthropic", model="claude-haiku-5-5", reasoning="high"
        )
        provider = AnthropicProvider(settings=frozen_settings(routes))
    else:
        from openai import APIConnectionError

        provider = OpenAIProvider(settings=frozen_settings(routes))
    attempts = []

    def lost_response(**kwargs):
        attempts.append(kwargs)
        raise APIConnectionError(
            request=httpx.Request("POST", "https://provider.invalid/inference")
        )

    provider._client = (
        SimpleNamespace(messages=SimpleNamespace(stream=lost_response))
        if native == "anthropic"
        else SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lost_response))
        )
    )
    for _ in range(2):
        with pytest.raises(GenerationCallFailed, match="vérifier"):
            _replay(item_id, provider, routes=routes).complete_json(**_kwargs())
    assert len(attempts) == 1
    with session_scope() as session:
        call = session.scalar(select(GenerationCall))
        assert call.state == "uncertain" and call.attempt == 1 and call.response_data is None


def test_failed_publication_rolls_back_dossier_and_completion_journal_without_rebuying(monkeypatch):
    job_id = _analyzed_job()
    provider = _CountingProvider()
    original_apply = Applier._do_apply

    def fail_after_journaling(self, **kwargs):
        original_callback = kwargs["publication_callback"]

        def callback(report, session):
            original_callback(report, session)
            session.flush()
            raise OSError("simulated publication transaction failure")

        return original_apply(self, **{**kwargs, "publication_callback": callback})

    with monkeypatch.context() as patch:
        patch.setattr(Applier, "_do_apply", fail_after_journaling)
        run_id = GenerationCoordinator(provider=provider).start([job_id], mode="immediate")
    before = provider.calls.copy()
    with session_scope() as session:
        item = session.scalar(select(GenerationItem))
        app_id = item.application_id
        assert item.state == "failed" and item.result is None
        app = session.get(Application, app_id)
        assert not app.cv_json and not app.documents and not app.cv_pdf_path
        assert not (get_settings().output_dir / str(app_id)).exists()
        assert all(call.state == "completed" for call in session.scalars(select(GenerationCall)))
    GenerationCoordinator(provider=provider).retry(run_id)
    assert provider.calls == before
    with session_scope() as session:
        item = session.scalar(select(GenerationItem))
        assert item.state == "completed" and item.application_id == app_id
        assert session.get(GenerationRun, run_id).state == "completed"
        assert session.get(Application, app_id).cv_json


@pytest.mark.parametrize("native", ["openai", "anthropic"])
def test_missing_native_api_key_fails_before_submission_and_remains_retryable(native):
    item_id = _seed()
    routes = _routes()
    if native == "anthropic":
        routes["document_review"].update(
            provider="anthropic", model="claude-haiku-5-5", reasoning="high"
        )
        provider = AnthropicProvider(settings=frozen_settings(routes))
    else:
        provider = OpenAIProvider(settings=frozen_settings(routes))
    with pytest.raises(GenerationCallFailed):
        _replay(item_id, provider, routes=routes).complete_json(**_kwargs())
    assert provider._client is None
    with write_session_scope() as session:
        call = session.scalar(select(GenerationCall))
        assert call.state == "failed" and call.attempt == 0 and call.response_data is None
        call.state, call.error = "prepared", ""
    corrected = _AnswerProvider()
    assert _replay(item_id, corrected, routes=routes).complete_json(**_kwargs()).issues == []
    assert corrected.calls == 1


@pytest.mark.parametrize("notes", [None, "Candidate note kept while work is paused"])
def test_stale_item_releases_empty_reservation_and_new_creation_is_available(notes):
    job_id = _analyzed_job()
    provider = _CountingProvider(stop_after="cv_adaptation")
    coordinator = GenerationCoordinator(provider=provider)
    run_id = coordinator.start([job_id], mode="immediate", stop_requested=lambda: provider.stopped)
    with write_session_scope() as session:
        item = session.scalar(select(GenerationItem))
        app_id = item.application_id
        session.get(Application, app_id).notes = notes
        session.get(Job, job_id).description += " Updated source text."
    acquired = provider.calls.copy()
    provider.stopped, provider.stop_after = False, None
    coordinator.retry(run_id)
    assert provider.calls == acquired
    assert job_id not in busy_job_ids()
    with session_scope() as session:
        item = session.scalar(select(GenerationItem))
        assert item.state == "stale"
        app = session.get(Application, app_id)
        if notes:
            assert app.notes == notes and reservation_is_stale(app)
        else:
            assert app is None
    new_provider = _CountingProvider()
    new_run = GenerationCoordinator(provider=new_provider).start([job_id], mode="immediate")
    with session_scope() as session:
        assert session.get(GenerationRun, new_run).state == "completed"
        app = session.scalar(select(Application).where(Application.job_id == job_id))
        assert app.cv_json and app.notes == notes


def test_legacy_empty_reservation_is_reused_with_notes_on_new_creation():
    job_id = _analyzed_job()
    with write_session_scope() as session:
        application = Application(
            job_id=job_id,
            notes="Legacy note",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        session.add(application)
        session.flush()
        app_id = application.id
    provider = _CountingProvider()
    run_id = GenerationCoordinator(provider=provider).start([job_id], mode="immediate")
    with session_scope() as session:
        item = session.scalar(select(GenerationItem))
        assert item.application_id == app_id and item.state == "completed"
        assert session.get(GenerationRun, run_id).state == "completed"
        assert session.get(Application, app_id).notes == "Legacy note"
    assert provider.calls["cv_adaptation"] == 1


def test_abandon_releases_claim_and_preserves_notes_and_paid_checkpoints_for_resume():
    job_id = _analyzed_job()
    provider = _CountingProvider(stop_after="cv_adaptation")
    coordinator = GenerationCoordinator(provider=provider)
    run_id = coordinator.start([job_id], mode="immediate", stop_requested=lambda: provider.stopped)
    with write_session_scope() as session:
        item = session.scalar(select(GenerationItem))
        app_id = item.application_id
        session.get(Application, app_id).notes = "Note retained after abandoning"
        response = session.scalar(select(GenerationCall)).response_data
    coordinator.abandon(run_id)
    assert job_id not in busy_job_ids()
    with job_operation([job_id], operation="offre disponible après libération"):
        pass
    with session_scope() as session:
        assert session.get(GenerationRun, run_id).state == "cancelled"
        call = session.scalar(select(GenerationCall))
        assert call.state == "completed" and call.response_data == response
        app = session.get(Application, app_id)
        assert app.notes == "Note retained after abandoning" and reservation_is_stale(app)
    provider.stopped, provider.stop_after = False, None
    coordinator.retry(run_id)
    assert provider.calls["cv_adaptation"] == 1
    with session_scope() as session:
        assert session.get(GenerationRun, run_id).state == "completed"
        app = session.scalar(select(Application).where(Application.job_id == job_id))
        assert app.cv_json and app.notes == "Note retained after abandoning"


def test_abandoned_old_run_cannot_steal_a_new_generation_claim():
    from smartapply.concurrency import TaskConflictError

    job_id = _analyzed_job()
    first_provider = _CountingProvider(stop_after="cv_adaptation")
    first = GenerationCoordinator(provider=first_provider)
    old_run = first.start([job_id], mode="immediate", stop_requested=lambda: first_provider.stopped)
    first.abandon(old_run)
    new_provider = _CountingProvider(stop_after="application_texts")
    new_run = GenerationCoordinator(provider=new_provider).start(
        [job_id],
        mode="immediate",
        stop_requested=lambda: new_provider.stopped,
    )
    with pytest.raises((ValueError, TaskConflictError)):
        first.retry(old_run)
    with session_scope() as session:
        assert session.get(GenerationRun, old_run).state == "cancelled"
        assert session.get(GenerationRun, new_run).state == "paused"
    new_provider.stopped, new_provider.stop_after = False, None
    GenerationCoordinator(provider=new_provider).retry(new_run)
    assert new_provider.calls["cv_adaptation"] == 0
    assert new_provider.calls["application_texts"] == 1
