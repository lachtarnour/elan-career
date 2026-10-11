"""Cooperative shutdown keeps completed analysis work and stops new calls."""

from collections import Counter
from datetime import datetime, timezone
from threading import Barrier, Event

import pytest
from sqlalchemy import select

from smartapply.database import session_scope, write_session_scope
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.models import Application, Job, JobDuplicateStatus, JobScore, JobStatus
from smartapply.database.scope_checkpoints import ScopeCheckpoint
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.llm import MockLLMProvider
from smartapply.llm.provider import LLMValidationError
from smartapply.pipeline import Pipeline
from smartapply.profile import get_profile
from tests.test_pipeline import _register_llm_responses

STAGES = ("job_scope_screening", "job_offer_extraction", "job_fit_assessment")


def _jobs(count=1):
    now = datetime.now(timezone.utc)
    with write_session_scope() as session:
        jobs = [Job(
            external_id=f"shutdown-{index}", source="manual",
            title=f"Data Scientist NLP {index}", source_company=f"Company {index}",
            location="Paris",
            description="Build RAG pipelines and productionize ML services with Python and PyTorch.",
            filtered_at=now, ranked_at=now,
            score=JobScore(components={
                "filter_disposition": "pending_relevance",
                "hard_reject": HardRejectResult(decisions=()).audit_payload(),
            }),
        ) for index in range(count)]
        session.add_all(jobs)
        session.flush()
        return [job.id for job in jobs]


class _Provider(MockLLMProvider):
    def __init__(self, after=None):
        super().__init__()
        self.after = after
        self.calls = Counter()

    def complete_json(self, **kwargs):
        self.calls[(kwargs["job_id"], kwargs["purpose"])] += 1
        result = super().complete_json(**kwargs)
        if self.after:
            self.after(kwargs)
        return result


@pytest.fixture(autouse=True)
def _responses():
    _register_llm_responses()


@pytest.mark.parametrize("stage", STAGES)
def test_shutdown_preserves_last_valid_response_and_restarts_without_repeating_it(
    isolated_db, stage,
):
    job_id = _jobs()[0]
    stopped = Event()
    provider = _Provider(lambda kwargs: stopped.set() if kwargs["purpose"] == stage else None)
    report = Pipeline(llm=provider, stop_requested=stopped.is_set).analyze_jobs([job_id])
    assert report.cancelled and not report.errors
    assert report.analyzed == int(stage == "job_fit_assessment")
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert session.scalar(select(ScopeCheckpoint.job_id).where(
            ScopeCheckpoint.job_id == job_id,
        )) == job_id
        assert (job.analysis is not None) == (stage == "job_fit_assessment")
        assert (job.analyzed_at is not None) == (stage == "job_fit_assessment")
        assert (session.get(AnalysisCheckpoint, job_id) is not None) == (
            stage == "job_offer_extraction"
        )
    # A new Pipeline simulates app restart. Mock providers have no response
    # cache, so only the durable checkpoints/completed analysis can avoid calls.
    provider.after = None
    resumed = Pipeline(llm=provider).analyze_jobs([job_id])
    assert resumed.analyzed == int(stage != "job_fit_assessment")
    assert resumed.already_analyzed == int(stage == "job_fit_assessment")
    assert provider.calls == Counter({(job_id, purpose): 1 for purpose in STAGES})
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is not None
        assert session.get(AnalysisCheckpoint, job_id) is None


def test_shutdown_keeps_previous_and_last_completed_offers_but_starts_no_queued_call(
    isolated_db,
):
    ids = _jobs(3)
    stopped = Event()
    fit_count = 0

    def after(kwargs):
        nonlocal fit_count
        if kwargs["purpose"] == "job_fit_assessment":
            fit_count += 1
            if fit_count == 2:
                stopped.set()

    provider = _Provider(after)
    pipeline = Pipeline(llm=provider, stop_requested=stopped.is_set)
    pipeline._processor.settings = pipeline._processor.settings.model_copy(
        update={"llm_max_concurrent": 1},
    )
    report = pipeline.analyze_jobs(ids)
    assert report.cancelled and report.analyzed == 2 and not report.errors
    assert fit_count == 2
    called_ids = {job_id for job_id, _ in provider.calls}
    assert len(called_ids) == 2
    with session_scope() as session:
        for job_id in ids:
            assert (session.get(Job, job_id).analysis is not None) == (job_id in called_ids)
    provider.after = None
    resumed = Pipeline(llm=provider).analyze_jobs(ids)
    assert resumed.already_analyzed == 2 and resumed.analyzed == 1
    assert provider.calls == Counter({(job_id, purpose): 1 for job_id in ids for purpose in STAGES})


def test_shutdown_drains_all_inflight_valid_fits_and_starts_no_queued_calls(isolated_db):
    ids = _jobs(5)
    stopped = Event()
    fits_received = Barrier(2)

    def after(kwargs):
        if kwargs["purpose"] == "job_fit_assessment":
            # Both responses are already validated when Stop arrives. Closing
            # drains both workers, including the one returning slightly later.
            fits_received.wait(timeout=5)
            stopped.set()

    provider = _Provider(after)
    pipeline = Pipeline(llm=provider, stop_requested=stopped.is_set)
    pipeline._processor.settings = pipeline._processor.settings.model_copy(
        update={"llm_max_concurrent": 2},
    )
    report = pipeline.analyze_jobs(ids)
    assert report.cancelled and report.analyzed == 2 and not report.errors
    called_ids = {job_id for job_id, _ in provider.calls}
    assert len(called_ids) == 2
    assert provider.calls == Counter({(job_id, purpose): 1 for job_id in called_ids for purpose in STAGES})
    with session_scope() as session:
        assert all((session.get(Job, job_id).analysis is not None) == (job_id in called_ids)
                   for job_id in ids)


@pytest.mark.parametrize("change", ["body", "archived", "duplicate", "application", "profile", "model"])
def test_stop_during_fit_still_rejects_obsolete_source_admission_profile_or_model(
    isolated_db, change,
):
    job_id = _jobs()[0]
    stopped = Event()
    profile = get_profile().model_copy(deep=True)

    def after(kwargs):
        if kwargs["purpose"] != "job_fit_assessment":
            return
        stopped.set()
        if change == "profile":
            profile.skills.categories[0].skills.append("New candidate fact")
        elif change == "model":
            provider._model = "different-fit-model"
        else:
            with write_session_scope() as session:
                job = session.get(Job, job_id)
                if change == "body":
                    job.description += " A different responsibility."
                elif change == "archived":
                    job.status, job.archived_at = JobStatus.ARCHIVED, datetime.now(timezone.utc)
                elif change == "duplicate":
                    job.duplicate_review_status = JobDuplicateStatus.PENDING
                else:
                    session.add(Application(
                        job_id=job_id, status=JobStatus.SENT,
                        form_submitted_at=datetime.now(timezone.utc), notes="User work",
                    ))

    provider = _Provider(after)
    report = Pipeline(profile=profile, llm=provider, stop_requested=stopped.is_set).analyze_jobs([job_id])
    assert report.cancelled and report.analyzed == 0 and len(report.errors) == 1
    assert provider.calls == Counter({(job_id, purpose): 1 for purpose in STAGES})
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is None
        assert session.get(AnalysisCheckpoint, job_id) is not None
        if change == "application":
            assert session.get(Job, job_id).application.notes == "User work"


def test_stop_during_invalid_fit_keeps_extraction_without_publishing(isolated_db):
    job_id = _jobs()[0]
    stopped = Event()

    def invalid_fit(**kwargs):
        stopped.set()
        raise LLMValidationError("Invalid fit response")

    MockLLMProvider.register("job_fit_assessment", invalid_fit)
    provider = _Provider()
    report = Pipeline(llm=provider, stop_requested=stopped.is_set).analyze_jobs([job_id])
    assert report.cancelled and report.analyzed == 0 and len(report.errors) == 1
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is None
        assert session.get(AnalysisCheckpoint, job_id) is not None
