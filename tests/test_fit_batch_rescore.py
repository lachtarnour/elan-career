"""Fit-only Batch publication preserves dossiers and cannot duplicate a POST."""

import json

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchWave
from smartapply.database.models import Job, JobFitHistory, LLMUsage
from smartapply.llm.batch import BatchResult, BatchSnapshot
from smartapply.pipeline.fit_batch_rescore import FitBatchRescore
from smartapply.pipeline.rescore import prepare_rescore_plan
from tests.test_evidence_fit import explicit_fit_routes as _explicit_fit_routes
from tests.test_evidence_fit import seeded_analysis, snapshot

explicit_fit_routes = _explicit_fit_routes


class Transport:
    requires_journal = True

    def __init__(self, *, timeout=False):
        self.timeout = timeout
        self.creates = []
        self.results = {}

    def upload(self, content):
        assert content
        return "input-reference"

    def create(self, reference, metadata, *, requests):
        # The persisted intent must be committed before crossing the API boundary.
        with session_scope() as session:
            wave = session.get(AnalysisBatchWave, metadata["elan_wave_id"])
            assert wave.state == "submitting" and wave.input_file_id == reference
        self.creates.append(requests)
        if self.timeout:
            raise TimeoutError("Unknown POST outcome")
        self.results = {
            request.custom_id: BatchResult(
                custom_id=request.custom_id,
                content=json.dumps(
                    {"fit_score": 0.6137, "match_reasons": ["Documented match"], "risks": []}
                ),
                model=request.body["model"],
                prompt_tokens=10,
                completion_tokens=10,
            )
            for request in requests
        }
        return BatchSnapshot(
            id="batch-real-interface",
            status="in_progress",
            input_file_id=reference,
            total=len(requests),
        )

    def retrieve(self, batch_id):
        return BatchSnapshot(
            id=batch_id,
            status="completed",
            input_file_id="input-reference",
            output_file_id="results",
            total=len(self.results),
            completed=len(self.results),
        )

    def recover(self, metadata, **kwargs):
        return []

    def download(self, file_id, *, metadata):
        return self.results


def prepared():
    job_id = seeded_analysis()
    plan = prepare_rescore_plan(job_ids=[job_id], force=True)
    return job_id, plan


def test_batch_only_score_publication_and_history_are_idempotent(isolated_db):
    job_id, plan = prepared()
    before = snapshot(job_id)
    transport = Transport()
    runner = FitBatchRescore(transport=transport)
    run_id = runner.prepare(plan, selected_ids=[job_id])
    runner.submit(run_id)
    assert len(transport.creates) == 1
    assert runner.sync(run_id)["state"] == "completed"
    after = snapshot(job_id)
    for part in ("job", "score", "application", "documents"):
        assert after[part] == before[part]
    assert after["analysis"]["fit_score"] == 0.6137
    with session_scope() as session:
        wave = session.scalars(
            select(AnalysisBatchWave).where(AnalysisBatchWave.run_id == run_id)
        ).one()
        wave_id = wave.id
    runner.import_wave(wave_id, transport.results)
    with session_scope() as session:
        assert len(session.scalars(select(JobFitHistory)).all()) == 1
        assert len(session.scalars(select(LLMUsage)).all()) == 1
    assert snapshot(job_id) == after


def test_ambiguous_batch_post_is_never_resent(isolated_db):
    job_id, plan = prepared()
    transport = Transport(timeout=True)
    runner = FitBatchRescore(transport=transport)
    run_id = runner.prepare(plan, selected_ids=[job_id])
    with pytest.raises(TimeoutError):
        runner.submit(run_id)
    runner.submit(run_id)
    runner.sync(run_id)
    assert len(transport.creates) == 1
    assert runner.status(run_id)["state"] == "rescore_attention"
    assert runner.status(run_id)["waves"][0]["state"] == "submitting"


def test_changed_analysis_rolls_back_publication_and_consumption(isolated_db):
    job_id, plan = prepared()
    transport = Transport()
    runner = FitBatchRescore(transport=transport)
    run_id = runner.prepare(plan, selected_ids=[job_id])
    runner.submit(run_id)
    with session_scope() as session:
        session.get(Job, job_id).analysis.fit_score = 0.1234
        wave = session.scalars(
            select(AnalysisBatchWave).where(AnalysisBatchWave.run_id == run_id)
        ).one()
        wave_id = wave.id
    with pytest.raises(ValueError, match="changed"):
        runner.import_wave(wave_id, transport.results)
    with session_scope() as session:
        assert session.get(Job, job_id).analysis.fit_score == 0.1234
        assert session.scalars(select(JobFitHistory)).all() == []
        assert session.scalars(select(LLMUsage)).all() == []
        assert session.get(AnalysisBatchWave, wave_id).consumed_ids == []
