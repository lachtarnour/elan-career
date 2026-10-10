"""Durable three-stage analysis against isolated SQLite and a fake Batch service."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.analysis_batches import (
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.models import Job, JobAnalysis, JobScore, LLMCache, LLMUsage
from smartapply.database.repository.llm_cache import cache_set, purge_expired_cache
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.llm import (
    JobFitDecisionEvaluation,
    MockLLMProvider,
    OfferAnalysis,
    OfferExtractionOutput,
)
from smartapply.llm.batch import BatchResult, BatchSnapshot
from smartapply.llm.cache import make_cache_key
from smartapply.llm.prompts import job_analysis, job_fit
from smartapply.llm.request_options import request_cache_extra, stage_effort
from smartapply.llm.schemas import JobScopeScreening
from smartapply.llm.usage import estimate_cost_usd
from smartapply.llm_policy import resolve_route
from smartapply.offers import build_analyzer_input
from smartapply.pipeline import Pipeline
from smartapply.pipeline.batch_analysis import coordinator as coordinator_module
from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
from smartapply.pipeline.batch_analysis.presentation import analysis_workspace
from smartapply.pipeline.batch_analysis.results import schema_for_request, validate_request_response
from smartapply.pipeline.errors import TaskCancelledError
from smartapply.pipeline.process.extraction_screening import extraction_offer_facts
from smartapply.pipeline.rescore import reusable_offer
from smartapply.profile import get_profile
from tests.fit_fixtures import evaluation_from_user

pytestmark = pytest.mark.usefixtures("isolated_db")


def offer(role="Machine Learning Engineer"):
    return OfferAnalysis(
        role_type=role,
        seniority="",
        domain="",
        main_tasks=["Build machine learning models"],
        required_skills=["Python"],
        nice_to_have=[],
        cv_keywords_to_include=["Python"],
    )


def extraction_offer(role="Machine Learning Engineer"):
    return OfferExtractionOutput.model_validate(
        {
            **offer(role).model_dump(),
            "seniority": "unknown",
        }
    )


def fit():
    from smartapply.llm import JobFitAssessment

    return JobFitAssessment(
        fit_score=0.7,
        match_reasons=["Documented Python work"],
        risks=[],
    )


class FakeTransport:
    """A provider that keeps running after the local coordinator is recreated."""

    def __init__(self, *, batch_prefix="batch"):
        self.batch_prefix = batch_prefix
        self.inputs = {}
        self.snapshots = {}
        self.outputs = {}
        self.created = []
        self.retrieved = []
        self.recovered = []
        self.cancelled = []
        self.create_error = None
        self.accept_before_error = False
        self.upload_error = None
        self.upload_attempts = 0

    def upload(self, content):
        self.upload_attempts += 1
        if self.upload_error is not None:
            raise self.upload_error
        file_id = f"input-{len(self.inputs) + 1}"
        self.inputs[file_id] = [json.loads(line) for line in content.decode().splitlines()]
        return file_id

    def create(self, input_file_id, metadata):
        batch_id = f"{self.batch_prefix}-{len(self.created) + 1}"
        self.created.append(batch_id)
        state = BatchSnapshot(
            id=batch_id,
            status="in_progress",
            input_file_id=input_file_id,
            total=len(self.inputs[input_file_id]),
            metadata=metadata,
            created_at=100,
            expires_at=86500,
        )
        if self.create_error is None or self.accept_before_error:
            self.snapshots[batch_id] = state
        if self.create_error is not None:
            raise self.create_error
        return state

    def retrieve(self, batch_id):
        self.retrieved.append(batch_id)
        return self.snapshots[batch_id]

    def recover(self, metadata):
        self.recovered.append(metadata)
        return [
            state
            for state in self.snapshots.values()
            if all(state.metadata.get(key) == value for key, value in metadata.items())
        ]

    def download(self, file_id):
        return self.outputs[file_id]

    def cancel(self, batch_id):
        self.cancelled.append(batch_id)
        state = replace(self.snapshots[batch_id], status="cancelling")
        self.snapshots[batch_id] = state
        return state

    def requests(self, batch_id):
        return self.inputs[self.snapshots[batch_id].input_file_id]

    def complete(
        self, batch_id, *, status="completed", failed=(), missing=(), invalid=(), answers=None
    ):
        successes, errors = {}, {}
        answers = answers or {}
        for record in self.requests(batch_id):
            custom_id = record["custom_id"]
            if custom_id in missing:
                continue
            if custom_id in failed:
                errors[custom_id] = BatchResult(
                    custom_id=custom_id,
                    error_code="provider_error",
                    error_message="Retry this request",
                )
                continue
            schema = record["body"]["response_format"]["json_schema"]["name"]
            answer = answers.get(custom_id) or (
                JobScopeScreening.model_validate(
                    {
                        "mission_screen": {
                            "mission_relation": "unknown",
                            "mission_evidence": "insufficient",
                        }
                    }
                )
                if schema == "JobScopeScreening"
                else OfferExtractionOutput.model_validate(
                    extraction_offer(f"Machine Learning Engineer {custom_id}").model_dump(
                        exclude={"mission_screen"}
                    )
                )
                if schema == "OfferExtractionOutput"
                else evaluation_from_user(record["body"]["messages"][1]["content"])
            )
            successes[custom_id] = BatchResult(
                custom_id=custom_id,
                content=(
                    "{}"
                    if custom_id in invalid
                    else answer
                    if isinstance(answer, str)
                    else answer.model_dump_json()
                ),
                prompt_tokens=100,
                completion_tokens=50,
                cached_prompt_tokens=80,
                model=record["body"]["model"],
                response_id=f"response-{custom_id}",
            )
        output_file_id = f"output-{batch_id}" if successes else None
        error_file_id = f"errors-{batch_id}" if errors else None
        if output_file_id:
            self.outputs[output_file_id] = successes
        if error_file_id:
            self.outputs[error_file_id] = errors
        self.snapshots[batch_id] = replace(
            self.snapshots[batch_id],
            status=status,
            output_file_id=output_file_id,
            error_file_id=error_file_id,
            completed=len(successes),
            failed=len(errors),
            completed_at=200,
        )


@pytest.fixture(autouse=True)
def explicit_batch_routes(monkeypatch, tmp_path):
    """Simulated batches use independent explicit JSON routes."""
    from tests.scope_fixtures import set_test_routes

    set_test_routes(
        monkeypatch,
        tmp_path,
        {
            purpose: {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "reasoning": "medium",
                "max_output_tokens": 6000,
            }
            for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment")
        },
    )


def seed_jobs(count=1, *, exact_copies=False, screened=True):
    with session_scope() as session:
        jobs = []
        for index in range(count):
            body = "Build machine learning models with Python."
            if not exact_copies:
                body += f" Team {index}."
            jobs.append(
                Job(
                    external_id=f"batch-test-{index}",
                    title="Machine Learning Engineer",
                    source_company="Example AI",
                    location="Paris",
                    description=body,
                    source="manual",
                    filtered_at=datetime.now(timezone.utc),
                    score=JobScore(
                        components={
                            "filter_disposition": "pending_relevance",
                            "hard_reject": HardRejectResult(decisions=()).audit_payload(),
                        }
                    ),
                )
            )
        session.add_all(jobs)
        session.flush()
        from tests.scope_fixtures import seed_neutral_scope

        if screened:
            for job in jobs:
                seed_neutral_scope(session, job)
        return [job.id for job in jobs]


def pipeline(stop_requested=None):
    return Pipeline(
        profile=get_profile(),
        llm=MockLLMProvider(),
        stop_requested=stop_requested,
    )


def journal(run_id):
    with session_scope() as session:
        run = session.get(AnalysisBatchRun, run_id)
        items = list(
            session.scalars(
                select(AnalysisBatchItem)
                .where(AnalysisBatchItem.run_id == run_id)
                .order_by(AnalysisBatchItem.id)
            )
        )
        waves = list(
            session.scalars(
                select(AnalysisBatchWave)
                .where(AnalysisBatchWave.run_id == run_id)
                .order_by(AnalysisBatchWave.created_at)
            )
        )
        session.expunge_all()
        return run, items, waves


def counts():
    with session_scope() as session:
        return {
            "analyses": session.scalar(select(func.count()).select_from(JobAnalysis)),
            "usage": session.scalar(select(func.count()).select_from(LLMUsage)),
            "paid": session.scalar(
                select(func.count()).select_from(LLMUsage).where(LLMUsage.cached.is_(False))
            ),
            "cached": session.scalar(
                select(func.count()).select_from(LLMUsage).where(LLMUsage.cached.is_(True))
            ),
            "cache": session.scalar(select(func.count()).select_from(LLMCache)),
        }


def finish_next(transport, coordinator, **kwargs):
    transport.complete(transport.created[-1], **kwargs)
    coordinator.sync()


def test_restart_resumes_two_dependent_waves_and_keeps_full_analysis():
    ids = seed_jobs(2)
    transport = FakeTransport()
    first = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = first.start(ids)
    assert len(transport.created) == 1
    assert counts()["analyses"] == 0
    restarted = BatchAnalysisCoordinator(transport=transport)
    finish_next(transport, restarted)
    assert len(transport.created) == 2
    run, items, waves = journal(run_id)
    assert [wave.phase for wave in waves] == ["extraction", "evaluation"]
    assert all(item.offer_data is not None for item in items)
    assert counts()["analyses"] == 0
    finish_next(transport, BatchAnalysisCoordinator(transport=transport))
    assert journal(run_id)[0].state == "completed"
    assert counts()["analyses"] == 2
    assert counts()["paid"] == 4
    with session_scope() as session:
        jobs = [session.get(Job, job_id) for job_id in ids]
        assert all(job.analysis.fit_score == 0.88 and job.analyzed_at is not None for job in jobs)
        assert all(job.ranked_at is None and job.score is not None for job in jobs)


def test_batch_screening_skips_only_the_distant_offer_before_extraction():
    ids = seed_jobs(2, screened=False)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    records = transport.requests(transport.created[-1])
    finish_next(
        transport,
        coordinator,
        answers={
            record["custom_id"]: JobScopeScreening(
                mission_screen={
                    "mission_relation": "clearly_outside",
                    "mission_evidence": "sufficient",
                }
            )
            if index
            else JobScopeScreening.model_validate(
                {
                    "mission_screen": {
                        "mission_relation": "adjacent",
                        "mission_evidence": "sufficient",
                    }
                }
            )
            for index, record in enumerate(records)
        },
    )
    assert len(transport.requests(transport.created[-1])) == 1
    assert transport.requests(transport.created[-1])[0]["custom_id"].startswith("extraction-")
    finish_next(transport, coordinator)
    finish_next(transport, coordinator)
    run, items, waves = journal(run_id)
    assert run.state == "completed"
    assert [item.state for item in items] == ["completed", "filtered_after_screening"]
    assert [wave.phase for wave in waves] == ["screening", "extraction", "evaluation"]
    assert items[1].offer_data is None
    assert items[1].scope_data["mission_screen"]["mission_relation"] == "clearly_outside"
    summary = next(run for run in analysis_workspace()["runs"] if run["id"] == run_id)
    assert summary["analyzed"] == summary["screened_out"] == 1
    with session_scope() as session:
        distant = session.get(Job, ids[1])
        assert distant.archived_at is not None and distant.analysis is None


def test_batch_with_only_screened_offers_completes_without_extraction_wave():
    ids = seed_jobs(2, exact_copies=True, screened=False)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    finish_next(
        transport,
        coordinator,
        answers={
            request["custom_id"]: JobScopeScreening(
                mission_screen={
                    "mission_relation": "clearly_outside",
                    "mission_evidence": "sufficient",
                }
            )
            for request in transport.requests(transport.created[-1])
        },
    )
    run, items, waves = journal(run_id)
    assert run.state == "completed"
    assert all(
        item.state == "filtered_after_screening" and item.offer_data is None for item in items
    )
    assert [wave.phase for wave in waves] == ["screening"]
    assert counts()["analyses"] == 0 and counts()["paid"] == 1
    summary = next(run for run in analysis_workspace()["runs"] if run["id"] == run_id)
    assert summary["screened_out"] == 2
    assert all(phase["status"] == "skipped" for phase in summary["phase_batches"][1:])


def test_batch_invalid_screen_still_schedules_extraction_and_evaluation():
    ids = seed_jobs(screened=False)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    custom_id = transport.requests(transport.created[-1])[0]["custom_id"]
    finish_next(
        transport,
        coordinator,
        answers={
            custom_id: '{"mission_screen":{"mission_relation":"sales","mission_evidence":"sufficient"}}'
        },
    )
    assert journal(run_id)[1][0].state == "extraction_pending"
    assert journal(run_id)[1][0].scope_data["status"] == "fail_open"
    finish_next(transport, coordinator)
    finish_next(transport, coordinator)
    assert journal(run_id)[0].state == "completed"
    with session_scope() as session:
        job = session.get(Job, ids[0])
        assert job.analysis is not None and job.archived_at is None


def test_batch_screen_write_error_still_schedules_extraction_and_evaluation(monkeypatch):
    from smartapply.pipeline.process import extraction_screening

    ids = seed_jobs(screened=False)
    original = extraction_screening.mark_archived

    def fail_after_archive(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("screen archive interrupted")

    monkeypatch.setattr(extraction_screening, "mark_archived", fail_after_archive)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    custom_id = transport.requests(transport.created[-1])[0]["custom_id"]
    finish_next(
        transport,
        coordinator,
        answers={
            custom_id: JobScopeScreening(
                mission_screen={
                    "mission_relation": "clearly_outside",
                    "mission_evidence": "sufficient",
                }
            )
        },
    )
    assert journal(run_id)[1][0].state == "extraction_pending"
    finish_next(transport, coordinator)
    finish_next(transport, coordinator)
    assert journal(run_id)[0].state == "completed"
    with session_scope() as session:
        job = session.get(Job, ids[0])
        assert job.analysis is not None and job.archived_at is None


def test_current_batch_extraction_schema_is_validated():
    extracted = OfferExtractionOutput.model_validate(
        {**offer().model_dump(), "seniority": "unknown", "offer_language": "en"}
    )
    body = {
        "response_format": {
            "json_schema": {
                "name": OfferExtractionOutput.__name__,
                "schema": OfferExtractionOutput.model_json_schema(),
            }
        }
    }
    assert schema_for_request(body) is OfferExtractionOutput
    assert validate_request_response(body, extracted.model_dump_json()) == extracted


def test_disjoint_runs_progress_independently_without_reserving_an_offer_twice():
    ids = seed_jobs(4)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    first = coordinator.start(ids[:2])
    second = coordinator.start(ids[2:])
    assert first != second and len(transport.created) == 2
    with pytest.raises(ValueError, match="Aucune offre disponible"):
        coordinator.start(ids[:2])
    assert len(transport.created) == 2

    transport.complete("batch-1")
    coordinator.sync()
    assert [wave.phase for wave in journal(first)[2]] == ["extraction", "evaluation"]
    assert [wave.phase for wave in journal(second)[2]] == ["extraction"]

    transport.complete("batch-3")
    coordinator.sync()
    assert journal(first)[0].state == "completed"
    assert journal(second)[0].state == "active"
    transport.complete("batch-2")
    coordinator.sync()
    transport.complete("batch-4")
    coordinator.sync()
    assert journal(second)[0].state == "completed"
    assert counts()["analyses"] == 4


def test_retry_does_not_reactivate_an_offer_reserved_by_another_run():
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    original = coordinator.start(ids)
    request_id = transport.requests("batch-1")[0]["custom_id"]
    transport.complete("batch-1", failed=[request_id])
    coordinator.sync()
    assert journal(original)[0].state == "failed"
    replacement = coordinator.start(ids)
    with pytest.raises(ValueError, match="Aucune offre disponible à reprendre"):
        coordinator.retry(original)
    assert journal(original)[0].state == "failed"
    assert journal(replacement)[0].state == "active"
    assert len(transport.created) == 2


def test_split_batches_show_individual_sizes_and_start_evaluation_after_all_extracts(monkeypatch):
    monkeypatch.setattr(
        coordinator_module, "split_requests", lambda requests: [[request] for request in requests]
    )
    ids = seed_jobs(2)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    assert len(transport.created) == 2
    phase = next(
        phase
        for phase in analysis_workspace()["runs"][0]["phase_batches"]
        if phase["phase"] == "extraction"
    )
    assert phase["batch_count"] == 2
    assert [(batch["offers"], batch["total"]) for batch in phase["batches"]] == [(1, 1), (1, 1)]

    transport.complete("batch-1")
    coordinator.sync()
    assert len(transport.created) == 2
    assert journal(run_id)[0].state == "active"
    transport.complete("batch-2")
    coordinator.sync()
    assert len(transport.created) == 4
    assert [wave.phase for wave in journal(run_id)[2]] == [
        "extraction",
        "extraction",
        "evaluation",
        "evaluation",
    ]
    for batch_id in ("batch-3", "batch-4"):
        transport.complete(batch_id)
    coordinator.sync()
    assert journal(run_id)[0].state == "completed"
    assert counts()["analyses"] == 2


def test_denied_first_split_upload_does_not_submit_later_batches(monkeypatch):
    class UploadDenied(Exception):
        status_code = 401

    monkeypatch.setattr(
        coordinator_module, "split_requests", lambda requests: [[request] for request in requests]
    )
    ids = seed_jobs(2)
    transport = FakeTransport()
    transport.upload_error = UploadDenied("Missing scopes: api.files.write")
    run_id = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline()).start(ids)
    assert journal(run_id)[0].state == "blocked"
    assert transport.upload_attempts == 1 and transport.created == []


def test_cancelled_preparation_creates_no_run_or_provider_batch():
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(
        transport=transport, pipeline=pipeline(stop_requested=lambda: True)
    )
    with pytest.raises(TaskCancelledError):
        coordinator.start(ids)
    assert transport.created == [] and transport.upload_attempts == 0
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AnalysisBatchRun)) == 0


def test_stop_requested_while_submitting_cancels_provider_batch_before_evaluation():
    ids = seed_jobs()
    stopped = False

    class StopAfterSubmit(FakeTransport):
        def create(self, input_file_id, metadata):
            nonlocal stopped
            snapshot = super().create(input_file_id, metadata)
            stopped = True
            return snapshot

    transport = StopAfterSubmit()
    coordinator = BatchAnalysisCoordinator(
        transport=transport, pipeline=pipeline(stop_requested=lambda: stopped)
    )
    run_id = coordinator.start(ids)
    run, _, waves = journal(run_id)
    assert run.cancel_requested and transport.cancelled == ["batch-1"]
    assert [wave.phase for wave in waves] == ["extraction"]


def test_repeated_sync_imports_each_custom_id_and_usage_only_once():
    ids = seed_jobs(2)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    finish_next(transport, coordinator)
    before = counts()
    for _ in range(3):
        BatchAnalysisCoordinator(transport=transport).sync()
    assert counts() == before
    assert len(transport.created) == 2
    finish_next(transport, coordinator)
    before = counts()
    coordinator.sync()
    coordinator.sync()
    assert counts() == before
    assert all(wave.state == "imported" for wave in journal(run_id)[2])
    assert all(len(wave.consumed_ids) == len(wave.requests) for wave in journal(run_id)[2])


def test_interrupted_import_rolls_back_usage_cache_and_journal_before_retry(monkeypatch):
    ids = seed_jobs(2)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    batch_id = transport.created[-1]
    transport.complete(batch_id)
    original = coordinator_module.import_result

    def interrupted_import(*args):
        original(*args)
        raise RuntimeError("Application interrupted inside the import transaction")

    monkeypatch.setattr(coordinator_module, "import_result", interrupted_import)
    coordinator.sync()
    assert counts()["usage"] == 0
    assert counts()["cache"] == 0
    assert all(item.offer_data is None for item in journal(run_id)[1])
    assert journal(run_id)[2][0].consumed_ids == []
    monkeypatch.setattr(coordinator_module, "import_result", original)
    coordinator.sync()
    assert counts()["usage"] == 2 and counts()["cache"] == 2
    assert len(transport.created) == 2  # The second wave, never another extraction.
    finish_next(transport, coordinator)
    assert counts()["analyses"] == 2 and counts()["paid"] == 4


def test_import_uses_batch_discount_and_preserves_cached_input_tokens():
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    coordinator.start(ids)
    finish_next(transport, coordinator)
    with session_scope() as session:
        usage = session.scalar(select(LLMUsage))
        assert usage.cost_usd == pytest.approx(
            estimate_cost_usd(
                resolve_route(get_llm_settings(), "job_offer_extraction").model,
                100,
                50,
                cached_prompt_tokens=80,
            )
            * 0.5
        )
        assert usage.cached_prompt_tokens == 80


def test_invalid_and_partial_extractions_only_evaluate_successful_offers():
    ids = seed_jobs(3)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    original = transport.requests(transport.created[-1])
    failed_id, invalid_id = original[1]["custom_id"], original[2]["custom_id"]
    finish_next(transport, coordinator, failed=[failed_id], invalid=[invalid_id])
    assert len(transport.requests(transport.created[-1])) == 1
    finish_next(transport, coordinator)
    run, items, _ = journal(run_id)
    assert run.state == "partial"
    assert [item.state for item in items] == ["completed", "extraction_failed", "extraction_failed"]
    assert counts()["analyses"] == 1
    assert counts()["cache"] == 2
    assert counts()["paid"] == 4  # Includes failures and invalid output once.


def test_retry_submits_only_failed_extractions_and_preserves_completed_jobs():
    ids = seed_jobs(2)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    failed_id = transport.requests(transport.created[-1])[1]["custom_id"]
    finish_next(transport, coordinator, failed=[failed_id])
    finish_next(transport, coordinator)
    assert journal(run_id)[0].state == "partial"
    with session_scope() as session:
        completed_at = session.get(Job, ids[0]).analyzed_at
    coordinator.retry(run_id)
    assert len(transport.requests(transport.created[-1])) == 1
    assert transport.requests(transport.created[-1])[0]["custom_id"] == failed_id
    finish_next(transport, coordinator)
    finish_next(transport, coordinator)
    assert journal(run_id)[0].state == "completed"
    assert counts()["analyses"] == 2
    with session_scope() as session:
        assert session.get(Job, ids[0]).analyzed_at == completed_at


def test_retry_failed_evaluation_reuses_successful_extraction():
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    finish_next(transport, coordinator)
    failed_id = transport.requests(transport.created[-1])[0]["custom_id"]
    finish_next(transport, coordinator, failed=[failed_id])
    assert journal(run_id)[0].state == "failed"
    coordinator.retry(run_id)
    assert len(transport.created) == 3
    request = transport.requests(transport.created[-1])[0]
    assert request["body"]["response_format"]["json_schema"]["name"] == "JobFitDecisionEvaluation"
    finish_next(transport, coordinator)
    assert journal(run_id)[0].state == "completed"
    assert counts()["analyses"] == 1


def test_expired_batch_keeps_successes_and_retries_only_missing_requests():
    ids = seed_jobs(2)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    missing_id = transport.requests(transport.created[-1])[1]["custom_id"]
    finish_next(transport, coordinator, status="expired", missing=[missing_id])
    assert len(transport.requests(transport.created[-1])) == 1
    finish_next(transport, coordinator)
    assert counts()["analyses"] == 1
    coordinator.retry(run_id)
    assert [request["custom_id"] for request in transport.requests(transport.created[-1])] == [
        missing_id
    ]


@pytest.mark.parametrize("phase", ["extraction", "evaluation"])
def test_offer_changes_during_wait_prevent_stale_publication(phase):
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    if phase == "evaluation":
        finish_next(transport, coordinator)
    with session_scope() as session:
        session.get(Job, ids[0]).description = "An updated offer requiring another analysis."
    finish_next(transport, coordinator)
    run, items, _ = journal(run_id)
    assert items[0].state == "skipped"
    assert "modifiée" in items[0].error
    assert counts()["analyses"] == 0
    assert len(transport.created) == (1 if phase == "extraction" else 2)
    assert run.state == "failed"


@pytest.mark.parametrize("phase", ["extraction", "evaluation"])
def test_profile_changes_during_wait_prevent_stale_publication(monkeypatch, phase):
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    if phase == "evaluation":
        finish_next(transport, coordinator)
    changed = get_profile().model_copy(deep=True)
    changed.identity.title = "A changed profile title"
    monkeypatch.setattr("smartapply.pipeline.batch_analysis.results.get_profile", lambda: changed)
    finish_next(transport, coordinator)
    item = journal(run_id)[1][0]
    assert item.state == "skipped"
    assert "profil a changé" in item.error
    assert counts()["analyses"] == 0
    assert len(transport.created) == (1 if phase == "extraction" else 2)


def test_new_deterministic_rejection_during_wait_blocks_evaluation():
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    with session_scope() as session:
        job = session.get(Job, ids[0])
        proof = HardRejectResult(decisions=()).audit_payload()
        proof["rejected"] = True
        job.score.components = {"filter_disposition": "rejected", "hard_reject": proof}
    finish_next(transport, coordinator)
    assert journal(run_id)[1][0].state == "skipped"
    assert counts()["analyses"] == 0
    assert len(transport.created) == 1


def test_submission_intent_survives_crash_after_remote_acceptance(monkeypatch):
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())

    def interrupted_save(*args):
        raise RuntimeError("Application interrupted before saving provider ID")

    monkeypatch.setattr(coordinator, "_save_snapshot", interrupted_save)
    run_id = coordinator.start(ids)
    wave = journal(run_id)[2][0]
    assert wave.state == "submitting" and wave.provider_batch_id is None
    assert len(transport.created) == 1
    restarted = BatchAnalysisCoordinator(transport=transport)
    restarted.sync()
    wave = journal(run_id)[2][0]
    assert wave.provider_batch_id == "batch-1"
    assert len(transport.created) == 1
    assert len(transport.recovered) == 1


def test_ambiguous_post_recovery_never_resubmits_even_when_no_match_found():
    ids = seed_jobs()
    transport = FakeTransport()
    transport.create_error = TimeoutError("Outcome unknown")
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    assert journal(run_id)[0].state == "attention"
    for _ in range(3):
        BatchAnalysisCoordinator(transport=transport).sync()
    coordinator.retry(run_id)
    assert len(transport.created) == 1
    assert journal(run_id)[2][0].state == "submitting"
    assert journal(run_id)[0].state == "attention"


def test_denied_upload_blocks_without_retrying_or_losing_the_reserved_run():
    class UploadDenied(Exception):
        status_code = 401

    ids = seed_jobs(2)
    transport = FakeTransport()
    transport.upload_error = UploadDenied("Missing scopes: api.files.write")
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    run, items, waves = journal(run_id)
    assert run.state == "blocked"
    assert "api.files.write" in run.last_error
    assert len(items) == 2 and all(item.state == "extraction_pending" for item in items)
    assert len(waves) == 1 and waves[0].state == "prepared"
    assert waves[0].input_file_id is None and waves[0].provider_batch_id is None
    assert transport.upload_attempts == 1 and transport.created == []
    coordinator.sync()
    assert transport.upload_attempts == 1
    with pytest.raises(ValueError, match="Aucune offre disponible"):
        coordinator.start(ids)

    transport.upload_error = None
    coordinator.retry(run_id)
    assert journal(run_id)[0].state == "active"
    assert transport.upload_attempts == 2 and len(transport.created) == 1


def test_existing_denied_upload_is_blocked_without_another_remote_attempt():
    class UploadDenied(Exception):
        status_code = 401

    ids = seed_jobs()
    transport = FakeTransport()
    transport.upload_error = UploadDenied("Missing scopes: api.files.write")
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    with session_scope() as session:
        run = session.get(AnalysisBatchRun, run_id)
        run.state = "active"
        run.last_error = "Error code: 401 - Missing scopes: api.files.write"
    coordinator.sync()
    assert journal(run_id)[0].state == "blocked"
    assert transport.upload_attempts == 1


def test_blocked_upload_can_be_cancelled_without_provider_request():
    class UploadDenied(Exception):
        status_code = 403

    ids = seed_jobs()
    transport = FakeTransport()
    transport.upload_error = UploadDenied("Missing scopes: api.files.write")
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    coordinator.cancel(run_id)
    assert journal(run_id)[0].state == "cancelled"
    assert transport.upload_attempts == 1 and transport.created == []


def test_ambiguous_post_recovers_batch_accepted_before_timeout():
    ids = seed_jobs()
    transport = FakeTransport()
    transport.create_error = TimeoutError("Outcome unknown")
    transport.accept_before_error = True
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    transport.create_error = None
    coordinator.sync()
    assert journal(run_id)[0].state == "active"
    assert journal(run_id)[2][0].provider_batch_id == "batch-1"
    assert len(transport.created) == 1


def test_multiple_metadata_matches_require_attention_without_another_post():
    ids = seed_jobs()
    transport = FakeTransport()
    transport.create_error = TimeoutError("Outcome unknown")
    transport.accept_before_error = True
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    transport.snapshots["another-batch"] = replace(
        transport.snapshots["batch-1"], id="another-batch"
    )
    coordinator.sync()
    run = journal(run_id)[0]
    assert run.state == "attention"
    assert "Plusieurs lots" in run.last_error
    assert len(transport.created) == 1


def test_cancel_extraction_preserves_received_results_without_submitting_evaluation():
    ids = seed_jobs(2)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    first_batch = transport.created[-1]
    coordinator.cancel(run_id)
    assert transport.cancelled == [first_batch]
    assert len(transport.created) == 1
    finish_next(transport, coordinator, status="cancelled")
    run, items, _ = journal(run_id)
    assert run.state == "cancelled"
    assert all(item.state == "cancelled" for item in items)
    assert all(item.offer_data is not None for item in items)
    assert counts()["analyses"] == 0
    assert len(transport.created) == 1
    with pytest.raises(ValueError, match="annulé"):
        coordinator.retry(run_id)


def test_exact_duplicate_prompts_pay_for_one_request_per_wave():
    ids = seed_jobs(2, exact_copies=True)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    assert len(transport.requests(transport.created[-1])) == 1
    assert len(journal(run_id)[2][0].requests[0]["item_ids"]) == 2
    finish_next(transport, coordinator)
    assert len(transport.requests(transport.created[-1])) == 1
    finish_next(transport, coordinator)
    assert counts()["analyses"] == 2
    assert counts()["paid"] == 2


def _write_sync_cache(job_id, *, extraction=True, evaluation=False, extraction_answer=None):
    profile = get_profile()
    model = resolve_route(get_llm_settings(), "job_offer_extraction").model
    with session_scope() as session:
        job = session.get(Job, job_id)
        requests = []
        if extraction:
            requests.append(
                (
                    "job_offer_extraction",
                    OfferExtractionOutput,
                    job_analysis.build_system_prompt(),
                    job_analysis.build_user_prompt_from_input(
                        analyzer_input=build_analyzer_input(job)
                    ),
                    extraction_answer or extraction_offer(),
                )
            )
        if evaluation:
            from smartapply.llm.fit_context import build_fit_context, build_fit_user

            fit_user = build_fit_user(
                build_fit_context(profile, model),
                extraction_offer_facts(extraction_offer()),
                extraction_provenance={"analyzer_input": asdict(build_analyzer_input(job))},
            )
            requests.append(
                (
                    "job_fit_assessment",
                    JobFitDecisionEvaluation,
                    job_fit.build_system_prompt(),
                    fit_user,
                    evaluation_from_user(fit_user),
                )
            )
        for purpose, schema, system, user, answer in requests:
            key = make_cache_key(
                model=model,
                system=system,
                user=user,
                schema_name=schema.__name__,
                extra=request_cache_extra(
                    schema,
                    0.2,
                    stage_effort(
                        "openai",
                        model,
                        purpose,
                    ),
                ),
            )
            cache_set(
                session,
                cache_key=key,
                model=model,
                purpose=purpose,
                response=answer if isinstance(answer, str) else answer.model_dump_json(),
                prompt_tokens=100,
                completion_tokens=50,
            )


def test_existing_synchronous_exact_cache_skips_extraction_batch():
    ids = seed_jobs()
    _write_sync_cache(ids[0])
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    assert len(transport.created) == 1
    assert journal(run_id)[2][0].phase == "evaluation"
    assert counts()["cached"] == 1
    finish_next(transport, coordinator)
    assert journal(run_id)[0].state == "completed"
    assert counts()["paid"] == 1


@pytest.mark.parametrize("from_cache", [False, True])
def test_batch_preserves_wrapped_source_quotes_through_evaluation_and_storage(from_cache):
    ids = seed_jobs()
    extracted = extraction_offer()
    source_quote = "Build machine learning models with Python."
    extracted.supporting_excerpts = [f"« {source_quote} »"]
    if from_cache:
        _write_sync_cache(ids[0], extraction_answer=extracted)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    if not from_cache:
        custom_id = transport.requests(transport.created[-1])[0]["custom_id"]
        finish_next(transport, coordinator, answers={custom_id: extracted})
    item = journal(run_id)[1][0]
    assert item.offer_data["supporting_excerpts"] == [source_quote]
    assert item.offer_data["offer_risks"] == extracted.offer_risks
    request = transport.requests(transport.created[-1])[0]
    assert request["body"]["response_format"]["json_schema"]["name"] == "JobFitDecisionEvaluation"
    assert json.dumps([source_quote]) in request["body"]["messages"][1]["content"]
    finish_next(transport, coordinator)
    assert journal(run_id)[0].state == "completed"
    assert len(transport.created) == (1 if from_cache else 2)
    assert counts()["paid"] == (1 if from_cache else 2)
    with session_scope() as session:
        job = session.get(Job, ids[0])
        assert job.analysis.fit_score == 0.88
        assert job.analysis.raw_response["supporting_excerpts"] == [source_quote]
        assert job.analysis.raw_response["offer_risks"] == extracted.offer_risks
        cached = session.scalar(select(LLMCache).where(LLMCache.purpose == "job_offer_extraction"))
        assert json.loads(cached.response)["supporting_excerpts"] == extracted.supporting_excerpts
    with session_scope() as session:
        assert (
            purge_expired_cache(
                session, ttl_days=15, now=datetime.now(timezone.utc) + timedelta(days=16)
            )
            > 0
        )
    with session_scope() as session:
        job = session.get(Job, ids[0])
        assert session.scalar(select(func.count()).select_from(LLMCache)) == 0
        source = job.analysis.fit_diagnostics["extraction_provenance"]
        assert source["analyzer_input"] == item.analyzer_input
        assert source["run_id"] == run_id
        assert job.analysis.fit_diagnostics["offer_facts"]["required_skills"] == ["Python"]
        reused, provenance = reusable_offer(session, job)
        assert reused is not None
        assert provenance == source


def test_all_cached_run_completes_without_any_upload_or_batch_creation():
    ids = seed_jobs()
    _write_sync_cache(ids[0], evaluation=True)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    assert transport.inputs == {} and transport.created == []
    assert journal(run_id)[0].state == "completed"
    assert journal(run_id)[2] == []
    assert counts()["analyses"] == 1
    assert counts()["cached"] == 2 and counts()["paid"] == 0


def test_unknown_output_ids_do_not_publish_or_record_usage():
    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    batch_id = transport.created[-1]
    transport.complete(batch_id)
    output = transport.outputs[transport.snapshots[batch_id].output_file_id]
    output["foreign-id"] = BatchResult(custom_id="foreign-id", content=offer().model_dump_json())
    coordinator.sync()
    assert counts()["usage"] == 0 and counts()["analyses"] == 0
    assert "étrangers" in journal(run_id)[0].last_error
    assert len(transport.created) == 1


@pytest.mark.parametrize("next_mode", ["batch", "immediate"])
def test_cancelled_extraction_resumes_evaluation_without_cache(next_mode):
    from sqlalchemy import delete

    from smartapply.database.analysis_checkpoints import AnalysisCheckpoint

    ids = seed_jobs()
    transport = FakeTransport()
    instance = pipeline()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=instance)
    old_run = coordinator.start(ids)
    coordinator.cancel(old_run)
    finish_next(transport, coordinator, status="cancelled")
    with session_scope() as session:
        session.execute(delete(LLMCache))
    assert analysis_workspace()["runs"] == []
    assert analysis_workspace()["candidates"][0]["state_label"] == "À évaluer"
    if next_mode == "batch":
        run_id = coordinator.start(ids)
        assert journal(run_id)[1][0].state == "evaluation_pending"
        assert transport.requests(transport.created[-1])[0]["custom_id"].startswith("evaluation-")
        finish_next(transport, coordinator)
    else:
        calls = []
        from types import SimpleNamespace

        instance.llm = SimpleNamespace(
            name="openai",
            cheap_model=resolve_route(coordinator.settings, "job_offer_extraction").model,
            _settings=coordinator.settings,
            complete_json=lambda **kwargs: (
                calls.append(kwargs["purpose"]) or evaluation_from_user(kwargs["user"])
            ),
        )
        instance._processor.llm = instance.llm
        assert instance.analyze_jobs(ids).analyzed == 1
        assert calls == ["job_fit_assessment"]
    with session_scope() as session:
        assert session.get(AnalysisCheckpoint, ids[0]) is None
    assert analysis_workspace()["candidates"] == []


def test_cancelled_journal_recovers_only_matching_extraction_contract():
    from sqlalchemy import delete

    from smartapply.database.analysis_checkpoints import AnalysisCheckpoint

    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    run_id = coordinator.start(ids)
    coordinator.cancel(run_id)
    finish_next(transport, coordinator, status="cancelled")
    with session_scope() as session:
        session.execute(delete(LLMCache))
        session.execute(delete(AnalysisCheckpoint))
    new_run = coordinator.start(ids)
    assert journal(new_run)[1][0].state == "evaluation_pending"
    assert transport.requests(transport.created[-1])[0]["custom_id"].startswith("evaluation-")


def test_immediate_stale_selection_cannot_duplicate_active_batch():
    ids = seed_jobs()
    transport = FakeTransport()
    instance = pipeline()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=instance)
    coordinator.start(ids)
    calls = []
    instance.llm.complete_json = lambda **kwargs: calls.append(kwargs)
    report = instance.analyze_jobs(ids)
    assert report.analyzed == 0 and report.skipped_missing == 1
    assert not calls
    assert not instance._processor._analyze_one(job_id=ids[0])
    assert not calls


def test_cancel_one_run_preserves_other_run_and_hides_only_confirmed_cancellation():
    ids = seed_jobs(2)
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    first = coordinator.start([ids[0]])
    second = coordinator.start([ids[1]])
    coordinator.cancel(first)
    assert transport.cancelled == ["batch-1"]
    assert {run["id"] for run in analysis_workspace()["runs"]} == {first, second}
    transport.complete(
        "batch-1", status="cancelled", missing=[transport.requests("batch-1")[0]["custom_id"]]
    )
    coordinator.sync()
    assert [run["id"] for run in analysis_workspace()["runs"]] == [second]
    assert journal(second)[0].state == "active"
    assert [row["id"] for row in analysis_workspace()["candidates"]] == [ids[0]]


def test_batch_start_conflicts_with_offer_owned_by_immediate_worker():
    from concurrent.futures import ThreadPoolExecutor

    from smartapply.concurrency import TaskConflictError, job_operation

    ids = seed_jobs()
    transport = FakeTransport()
    coordinator = BatchAnalysisCoordinator(transport=transport, pipeline=pipeline())
    with (
        job_operation(ids, operation="analyse immédiate"),
        ThreadPoolExecutor(max_workers=1) as pool,
        pytest.raises(TaskConflictError),
    ):
        pool.submit(coordinator.start, ids).result(timeout=5)
    assert transport.created == []
    assert analysis_workspace()["runs"] == []
