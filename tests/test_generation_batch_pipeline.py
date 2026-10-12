"""Production document generation through successive native Batch waves."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.generation_runs import (
    GenerationCall,
    GenerationItem,
    GenerationRun,
    GenerationWave,
)
from smartapply.database.models import Application, JobStatus, LLMUsage
from smartapply.llm import DocumentTextReview, MockLLMProvider
from smartapply.llm.anthropic_batch import AnthropicBatchTransport
from smartapply.offers import ManualOfferInput
from smartapply.pipeline import Pipeline
from smartapply.pipeline.generation.coordinator import GenerationCoordinator
from smartapply.pipeline.generation.replay import PURPOSES
from smartapply.pipeline.generation.transport import SCHEMAS
from tests.llm_config_helpers import configure_calls
from tests.test_batch_analysis import FakeTransport
from tests.test_pipeline import _register_llm_responses
from tests.test_provider_batch_analysis import NativeClaudeClient


@pytest.fixture
def analyzed_jobs(isolated_db):
    _register_llm_responses()

    def create(count=1):
        pipeline = Pipeline(llm=MockLLMProvider())
        descriptions = (
            "Build RAG pipelines and productionize ML services with Python and PyTorch.",
            "Evaluate clinical retrieval for healthcare teams using large language models and Python.",
        )
        ids = []
        for index in range(count):
            report = pipeline.ingest_manual_offer(ManualOfferInput(
                title=f"Data Scientist NLP {index}", company=f"HealthTech {index}",
                location="Paris", description=descriptions[index],
                application_url=f"https://healthtech.example/job/{index}",
            ))
            ids.extend(report.job_ids)
        pipeline.prepare_analysis(job_ids=ids, exclude_applications=False)
        assert pipeline.analyze_jobs(ids, exclude_applications=False).analyzed == count
        return ids

    return create


def native_routes(monkeypatch, tmp_path, *, mixed=False):
    configure_calls(monkeypatch, tmp_path, **{
        purpose: {
            "provider": "anthropic" if mixed and purpose == "application_texts" else "openai",
            "model": "claude-haiku-5-5" if mixed and purpose == "application_texts" else "gpt-5.4-mini",
            "reasoning": "high" if mixed and purpose == "application_texts" else "medium",
            "max_output_tokens": 32000,
        }
        for purpose in PURPOSES
    })


def journal(run_id):
    with session_scope() as session:
        items = list(session.scalars(select(GenerationItem).where(
            GenerationItem.run_id == run_id,
        ).order_by(GenerationItem.id)))
        calls = list(session.scalars(select(GenerationCall).join(GenerationItem).where(
            GenerationItem.run_id == run_id,
        ).order_by(GenerationCall.id)))
        waves = list(session.scalars(select(GenerationWave).where(
            GenerationWave.run_id == run_id,
        ).order_by(GenerationWave.created_at)))
        return session.get(GenerationRun, run_id), items, calls, waves


class Harness:
    def __init__(self, monkeypatch, *, answer=None):
        self.openai = FakeTransport()
        self.native = NativeClaudeClient()
        self.anthropic = AnthropicBatchTransport(self.native)
        self.provider = MockLLMProvider()
        self.answer = answer
        self.completed = set()
        self.submitted = Counter()
        transports = {"openai": self.openai, "anthropic": self.anthropic}
        monkeypatch.setattr(
            "smartapply.pipeline.generation.transport.get_batch_transport",
            lambda provider, *, settings: transports[provider],
        )

    def coordinator(self):
        return GenerationCoordinator(provider=self.provider)

    def complete(self, run_id, *, failed=(), status="completed", missing=()):
        _, _, calls, waves = journal(run_id)
        by_id = {call.id: call for call in calls}
        for wave in waves:
            if wave.state != "submitted" or wave.id in self.completed:
                continue
            answers = {}
            for record in wave.requests:
                call = by_id[record["call_id"]]
                body = record["body"]
                self.submitted[call.purpose] += 1
                response = self.provider.complete_json(
                    system=body["messages"][0]["content"],
                    user=body["messages"][1]["content"], schema=SCHEMAS[call.schema_name],
                    purpose=("cv_adaptation" if call.purpose == "cv_selection_correction"
                             else call.purpose),
                ) if call.purpose != "document_correction" else DocumentTextReview(issues=[])
                if self.answer:
                    response = self.answer(call, response)
                answers[record["custom_id"]] = response
            if wave.provider == "anthropic":
                assert not failed and not missing and status == "completed"
                self.native.complete(wave.provider_batch_id, answers=answers)
            else:
                self.openai.complete(
                    wave.provider_batch_id, answers=answers, failed=failed,
                    status=status, missing=missing,
                )
            self.completed.add(wave.id)

    def finish(self, run_id, *, max_waves=8):
        for _ in range(max_waves):
            run, _, _, _ = journal(run_id)
            if run.state == "completed":
                return
            self.complete(run_id)
            self.coordinator().sync()
        assert journal(run_id)[0].state == "completed", journal(run_id)[0].last_error


@pytest.mark.parametrize("mixed", [False, True])
def test_real_batch_pipeline_automatically_chains_native_stages_and_python(
    analyzed_jobs, monkeypatch, tmp_path, mixed,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path, mixed=mixed)
    harness = Harness(monkeypatch)
    run_id = harness.coordinator().start(ids, mode="batch")
    _, items, calls, _ = journal(run_id)
    app_id = items[0].application_id
    assert app_id is not None and [call.purpose for call in calls] == ["cv_adaptation"]
    harness.finish(run_id)
    run, items, calls, waves = journal(run_id)
    assert run.state == "completed" and items[0].application_id == app_id
    assert [call.purpose for call in calls] == [
        "cv_adaptation", "application_texts", "document_review",
    ]
    assert all(call.state == "completed" for call in calls)
    assert [wave.provider for wave in waves] == [
        "openai", "anthropic" if mixed else "openai", "openai",
    ]
    with session_scope() as session:
        app = session.get(Application, app_id)
        assert app.status == JobStatus.READY_FOR_FORM_SUBMISSION
        assert app.cv_json["selected_project_ids"] and app.cv_json["selected_experiences"]
        assert app.documents and all(Path(doc.path).is_file() for doc in app.documents if doc.path)
        assert app.cv_docx_path and Path(app.cv_docx_path).is_file()
        assert app.cv_pdf_path and Path(app.cv_pdf_path).is_file()
        usages = list(session.scalars(select(LLMUsage).where(LLMUsage.purpose.in_(PURPOSES))))
        assert len(usages) == 3
        assert {usage.provider for usage in usages} == ({"openai", "anthropic"} if mixed else {"openai"})
    harness.coordinator().sync()
    assert harness.submitted == Counter(cv_adaptation=1, application_texts=1, document_review=1)


def test_selection_correction_automatically_runs_before_writing(analyzed_jobs, monkeypatch, tmp_path):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)

    def answer(call, response):
        if call.purpose == "cv_adaptation":
            return response.model_copy(update={"selected_project_ids": ["proj_evidence_rag"]})
        return response

    harness = Harness(monkeypatch, answer=answer)
    run_id = harness.coordinator().start(ids, mode="batch")
    harness.finish(run_id)
    assert [call.purpose for call in journal(run_id)[2]] == [
        "cv_adaptation", "cv_selection_correction", "application_texts", "document_review",
    ]
    assert all(count == 1 for count in harness.submitted.values())


@pytest.mark.parametrize("mode", ["immediate", "batch"])
@pytest.mark.parametrize(
    ("defect", "message", "point_key"),
    [("project_count", "too_few_projects", "projects_minimum"),
     ("project_evidence", "ungrounded_project_evidence", "projects_evidence"),
     ("certificate", "unknown_certificate_id", "certificates_unknown"),
     ("publisher", "publisher_reintroduced", "document_company")],
)
def test_python_content_findings_publish_ready_dossier_with_visible_messages(
    analyzed_jobs, monkeypatch, tmp_path, mode, defect, message, point_key,
):
    from smartapply.database import write_session_scope
    from smartapply.database.models import Job
    from smartapply.desktop.services import DesktopService

    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    if defect == "publisher":
        with write_session_scope() as session:
            job = session.get(Job, ids[0])
            job.analysis.raw_response = {
                **job.analysis.raw_response, "extracted_company_name": "LinkedIn",
            }

    def change(purpose, response):
        if purpose in {"cv_adaptation", "cv_selection_correction"}:
            if defect == "project_count":
                return response.model_copy(update={"selected_project_ids": ["proj_evidence_rag"]})
            if defect == "project_evidence":
                response = response.model_copy(deep=True)
                response.project_selection_reasons[0].offer_evidence = "Invented offer demand"
            if defect == "certificate":
                return response.model_copy(update={"selected_certificate_ids": ["unknown"]})
        if defect == "publisher" and purpose == "application_texts":
            return response.model_copy(update={"motivation_letter_subject": "Application to LinkedIn"})
        return response

    harness = Harness(monkeypatch, answer=lambda call, response: change(call.purpose, response))
    if mode == "immediate":
        original = harness.provider.complete_json

        def respond(**kwargs):
            purpose = kwargs["purpose"]
            response = original(**{
                **kwargs, "purpose": "cv_adaptation" if purpose == "cv_selection_correction" else purpose,
            })
            return change(purpose, response)

        harness.provider.complete_json = respond
    run_id = harness.coordinator().start(ids, mode=mode)
    if mode == "batch":
        harness.finish(run_id)
    run, items, calls, _ = journal(run_id)
    assert run.state == "completed" and items[0].state == "completed"
    assert {"application_texts", "document_review"} <= {call.purpose for call in calls}
    with session_scope() as session:
        app = session.get(Application, items[0].application_id)
        assert app.status == JobStatus.READY_FOR_FORM_SUBMISSION
        assert any(message in warning for warning in app.validation_warnings)
        assert app.documents and all(Path(doc.path).is_file() for doc in app.documents if doc.path)
        app_id = app.id
    detail = DesktopService().get_application(app_id)
    assert detail.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert point_key in {point["key"] for point in detail.review_points}


def test_review_correction_and_confirmation_preserve_assembled_selection(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    finding = {
        "field": "cv_title", "passage": "Principal ",
        "reason": "The candidate evidence does not support this seniority.",
        "source_ids": ["candidate:provided_evidence"], "category": "factual",
        "replacement": None,
    }

    def answer(call, response):
        if call.purpose == "cv_adaptation":
            return response.model_copy(update={"cv_title": "Principal " + response.cv_title})
        if call.purpose == "document_review" and call.position == 2:
            return DocumentTextReview(issues=[finding])
        if call.purpose == "document_correction":
            return DocumentTextReview(issues=[{**finding, "replacement": ""}])
        return response

    harness = Harness(monkeypatch, answer=answer)
    run_id = harness.coordinator().start(ids, mode="batch")
    harness.finish(run_id)
    _, items, calls, _ = journal(run_id)
    assert [call.purpose for call in calls] == [
        "cv_adaptation", "application_texts", "document_review",
        "document_correction", "document_review",
    ]
    assert [trace["decision"] for trace in items[0].result["document_review"]] == [
        "correction_requested", "revision_proposed", "accepted",
    ]
    with session_scope() as session:
        cv = session.get(Application, items[0].application_id).cv_json
        assert not cv["cv_title"].startswith("Principal ")
        assert cv["selected_project_ids"] == [
            "proj_evidence_rag", "proj_ner_camembert", "proj_smartapply",
        ]


def test_partial_failure_retry_preserves_successful_applications_and_paid_calls(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs(2)
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    coordinator = harness.coordinator()
    run_id = coordinator.start(ids, mode="batch")
    waves = journal(run_id)[3]
    failed_id = waves[0].requests[1]["custom_id"]
    harness.complete(run_id, failed=[failed_id])
    coordinator.sync()
    # The surviving offer completes all following steps without waiting for retry.
    harness.complete(run_id)
    coordinator.sync()
    harness.complete(run_id)
    coordinator.sync()
    run, items, calls, _ = journal(run_id)
    assert run.state == "partial" and [item.state for item in items] == ["completed", "failed"]
    finished_app_id = items[0].application_id
    original_completed = {call.id: call.response_data for call in calls if call.state == "completed"}
    coordinator.retry(run_id)
    harness.finish(run_id)
    _, items, calls, waves = journal(run_id)
    assert items[0].application_id == finished_app_id
    assert {call.id: call.response_data for call in calls if call.id in original_completed} == original_completed
    assert sum(len(wave.requests) for wave in waves) == 7
    assert harness.submitted == Counter(cv_adaptation=3, application_texts=2, document_review=2)


def test_cancel_resume_keeps_received_call_and_application_reservation(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    coordinator = harness.coordinator()
    run_id = coordinator.start(ids, mode="batch")
    app_id = journal(run_id)[1][0].application_id
    coordinator.cancel(run_id)
    assert harness.openai.cancelled == ["batch-1"]
    harness.complete(run_id, status="cancelled")
    coordinator.sync()
    run, items, calls, _ = journal(run_id)
    assert run.state == "paused" and items[0].application_id == app_id
    assert calls[0].state == "completed" and len(calls) == 1
    harness.coordinator().retry(run_id)
    harness.finish(run_id)
    assert journal(run_id)[1][0].application_id == app_id
    assert harness.submitted == Counter(cv_adaptation=1, application_texts=1, document_review=1)


def test_unknown_submission_recovers_then_automatically_continues(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    harness.openai.create_error = TimeoutError("lost reply after remote acceptance")
    harness.openai.accept_before_error = True
    run_id = harness.coordinator().start(ids, mode="batch")
    assert journal(run_id)[0].state == "attention"
    # Lose local knowledge of the submitted ID, leaving recovery to the journal.
    for _ in range(3):
        harness.coordinator().sync()
    assert journal(run_id)[1][0].state == "waiting"
    assert harness.openai.created == ["batch-1"]
    harness.openai.create_error = None
    # Recover has already found the in-progress batch for OpenAI. Completing it
    # now must advance to the next step without any explicit retry action.
    harness.finish(run_id)
    assert harness.submitted == Counter(cv_adaptation=1, application_texts=1, document_review=1)


@pytest.mark.parametrize("moment", ["upload", "create", "unknown_create"])
def test_stop_during_submission_is_durable_and_preserves_replay(
    analyzed_jobs, monkeypatch, tmp_path, moment,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    stopped = Event()
    transport = FakeTransport()
    if moment == "upload":
        original = transport.upload

        def upload(content):
            file_id = original(content)
            stopped.set()
            return file_id

        transport.upload = upload
    else:
        original = transport.create
        if moment == "unknown_create":
            transport.accept_before_error = True
            transport.create_error = TimeoutError("Lost accepted reply")

        def create(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                stopped.set()

        transport.create = create
    coordinator = GenerationCoordinator(transport=transport)
    run_id = coordinator.start(ids, mode="batch", stop_requested=stopped.is_set)
    run, items, calls, waves = journal(run_id)
    assert run.cancel_requested is True
    assert items[0].application_id is not None
    assert len(calls) == 1 and len(waves) == 1
    if moment == "upload":
        assert transport.created == transport.cancelled == []
        assert run.state == "paused"
        assert calls[0].state == "prepared" and calls[0].wave_id is None
        assert waves[0].state == "cancelled"
        # Restart and explicit retry can send the retained request once.
        resumed_transport = FakeTransport()
        GenerationCoordinator(transport=resumed_transport).retry(run_id)
        assert resumed_transport.created == ["batch-1"]
        assert journal(run_id)[0].cancel_requested is False
    else:
        assert transport.created == transport.cancelled == ["batch-1"]
        assert run.state == "active" and waves[0].state == "submitted"
        assert waves[0].provider_data["status"] == "cancelling"
        coordinator.sync()
        assert transport.created == ["batch-1"]


def test_different_dependent_stages_share_wave_without_skipping_python_validation(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs(2)
    native_routes(monkeypatch, tmp_path)

    def answer(call, response):
        if call.purpose == "cv_adaptation":
            with session_scope() as session:
                item = session.get(GenerationItem, call.item_id)
                if item.job_id == ids[0]:
                    return response.model_copy(update={"selected_project_ids": ["proj_evidence_rag"]})
        return response

    harness = Harness(monkeypatch, answer=answer)
    run_id = harness.coordinator().start(ids, mode="batch")
    harness.complete(run_id)
    harness.coordinator().sync()
    _, _, calls, waves = journal(run_id)
    by_id = {call.id: call for call in calls}
    latest = waves[-1]
    assert {by_id[record["call_id"]].purpose for record in latest.requests} == {
        "cv_selection_correction", "application_texts",
    }
    harness.finish(run_id)
    assert harness.submitted == Counter(
        cv_adaptation=2, cv_selection_correction=1, application_texts=2, document_review=2,
    )
