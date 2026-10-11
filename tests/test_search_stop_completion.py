"""Stopping remote search still completes bookkeeping for retained offers."""

from threading import Event

import pytest

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Job, JobStatus
from smartapply.desktop.services import DesktopService
from smartapply.llm import MockLLMProvider
from smartapply.offers import RawJob
from smartapply.pipeline import Pipeline
from smartapply.pipeline.batch_analysis.presentation import analysis_workspace
from smartapply.pipeline.errors import TaskCancelledError
from smartapply.pipeline.ingest import IngestCollection


@pytest.mark.parametrize("moment", ["collection", "persistence", "filtering"])
def test_cancelled_search_finishes_local_gates_for_only_retained_rows(
    isolated_db, monkeypatch, moment,
):
    stopped = Event()
    provider = MockLLMProvider()

    def unexpected_provider(**kwargs):
        pytest.fail("Search completion must never call an LLM")

    monkeypatch.setattr(provider, "complete_json", unexpected_provider)
    pipeline = Pipeline(llm=provider, stop_requested=stopped.is_set)
    with write_session_scope() as session:
        unrelated = Job(
            external_id="old-unfiltered", title="Unrelated legacy offer",
            source_company="Unrelated Company", source="serpapi",
            description="Unrelated historical content.",
        )
        session.add(unrelated)
        session.flush()
        unrelated_id = unrelated.id
    raw_jobs = [
        RawJob(
            external_id="linkedin:retained-ai", title="AI Engineer",
            company="Independent Machine Learning Company", source="linkedin",
            location="Paris", contract_type="CDI",
            description="Develop machine learning models using Python. CDI.",
        ),
        RawJob(
            external_id="linkedin:retained-internship", title="Stage Machine Learning Engineer",
            company="Internship Company", source="linkedin", location="Paris",
            contract_type="stage", description="Stage de 6 mois en machine learning avec Python.",
        ),
    ]

    def collect(*args, **kwargs):
        if moment == "collection":
            stopped.set()
        return IngestCollection(
            source="linkedin", raw_jobs=raw_jobs, cancelled=stopped.is_set(),
        )

    original_persist = pipeline.persist_collection

    def persist(collection):
        report = original_persist(collection)
        if moment == "persistence":
            stopped.set()
        return report

    original_evaluate = pipeline.filter.evaluate

    def evaluate(job):
        if moment == "filtering":
            stopped.set()
        return original_evaluate(job)

    monkeypatch.setattr(pipeline, "collect_source", collect)
    monkeypatch.setattr(pipeline, "persist_collection", persist)
    monkeypatch.setattr(pipeline.filter, "evaluate", evaluate)
    monkeypatch.setattr("smartapply.desktop.services._pipeline", lambda _=None: pipeline)
    service = DesktopService()
    monkeypatch.setattr(service, "source_diagnostics_stale", lambda: False)
    result = service.search_jobs(
        query="AI Engineer", location="Paris", sources=["linkedin"],
        max_results=2, date_posted="week", stop_requested=stopped.is_set,
    )
    assert result.cancelled and result.persisted == 2 and not result.errors
    with session_scope() as session:
        retained = session.query(Job).filter_by(external_id="linkedin:retained-ai").one()
        rejected = session.query(Job).filter_by(external_id="linkedin:retained-internship").one()
        retained_id = retained.id
        assert retained.filtered_at is not None and retained.archived_at is None
        assert rejected.status == JobStatus.ARCHIVED and rejected.archived_at is not None
        assert session.get(Job, unrelated_id).filtered_at is None
    assert [job.id for job in service.list_jobs(status=JobStatus.SCRAPED)] == [retained_id]
    assert service.pending_analysis_ids() == [retained_id]
    assert analysis_workspace()["candidates_count"] == 1
    # Completion does not weaken cancellation of later analysis/filter calls.
    with pytest.raises(TaskCancelledError):
        pipeline.filter_pending(job_ids=[unrelated_id])
