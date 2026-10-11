"""Submission, recovery and usage guarantees against an isolated database."""

from __future__ import annotations

import hashlib
import json
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from smartapply.database import session_scope, write_session_scope
from smartapply.database.generation_runs import (
    GenerationCall,
    GenerationItem,
    GenerationRun,
    GenerationWave,
)
from smartapply.database.models import Job, JobAnalysis, LLMUsage
from smartapply.llm.anthropic_batch import AnthropicBatchTransport
from smartapply.llm.batch import BatchResult, build_batch_request
from smartapply.llm.schemas import DocumentTextReview
from smartapply.pipeline.generation.locking import generation_operation
from smartapply.pipeline.generation.transport import GenerationBatchTransport
from smartapply.pipeline.snapshots import _digest, job_snapshot_key
from tests.test_batch_analysis import FakeTransport
from tests.test_provider_batch_analysis import NativeClaudeClient

pytestmark = pytest.mark.usefixtures("isolated_db")


def seed(count=1, *, provider="openai"):
    run_id = str(uuid4())
    model = "claude-haiku-5-5" if provider == "anthropic" else "gpt-5.4-mini"
    with write_session_scope() as session:
        session.add(GenerationRun(
            id=run_id, mode="batch", state="active", profile_data={}, routes={},
            reference_date="2026-10-11",
        ))
        session.flush()
        for position in range(count):
            job = Job(
                external_id=f"generation-{run_id}-{position}", title="AI Engineer",
                source_company="Acme", source="manual", description="Build AI systems",
            )
            session.add(job)
            session.flush()
            analysis = JobAnalysis(job_id=job.id, role_type="AI Engineer")
            job.analysis = analysis
            session.add(analysis)
            session.flush()
            session.refresh(analysis)
            item = GenerationItem(
                run_id=run_id, job_id=job.id, job_snapshot=job_snapshot_key(job),
                analysis_snapshot=_digest({column.name: getattr(analysis, column.name)
                                           for column in analysis.__table__.columns}),
                title=job.title, company=job.source_company,
                state="waiting",
            )
            session.add(item)
            session.flush()
            request = build_batch_request(
                custom_id=f"review-{position}", system="Review facts.", user=f"Text {position}",
                schema=DocumentTextReview, provider=provider, model=model,
                max_completion_tokens=1234, purpose="document_review",
                effort="high" if provider == "anthropic" else "medium",
            )
            session.add(GenerationCall(
                item_id=item.id, position=0, purpose="document_review",
                schema_name=DocumentTextReview.__name__, provider=provider,
                request_body=request.body,
                request_key=hashlib.sha256(json.dumps(request.body, sort_keys=True).encode()).hexdigest(),
            ))
    return run_id


def journal(run_id):
    with session_scope() as session:
        return (
            session.get(GenerationRun, run_id),
            list(session.scalars(select(GenerationCall).order_by(GenerationCall.id))),
            list(session.scalars(select(GenerationWave).where(GenerationWave.run_id == run_id))),
            session.scalar(select(func.count(LLMUsage.id))),
        )


def complete(transport, batch_id, **kwargs):
    transport.complete(batch_id, answers={
        request["custom_id"]: DocumentTextReview(issues=[])
        for request in transport.requests(batch_id)
    }, **kwargs)


def test_restart_imports_validated_responses_and_usage_only_once():
    run_id = seed(2)
    transport = FakeTransport()
    GenerationBatchTransport(transport=transport).submit_pending(run_id)
    _, calls, waves, usages = journal(run_id)
    assert len(waves) == 1 and len(transport.created) == 1 and usages == 0
    assert all(call.state == "submitted" for call in calls)
    complete(transport, "batch-1")
    resumed = GenerationBatchTransport(transport=transport)
    resumed.sync(run_id)
    resumed.sync(run_id)
    resumed.submit_pending(run_id)
    _, calls, waves, usages = journal(run_id)
    assert not resumed.outstanding(run_id)
    assert usages == 2 and transport.created == ["batch-1"]
    assert all(call.state == "completed" and call.response_data == {"issues": []} for call in calls)
    assert len(waves[0].consumed_ids) == 2


@pytest.mark.parametrize("accepted", [False, True])
def test_unknown_post_is_recovered_without_resending(accepted):
    run_id = seed()
    transport = FakeTransport()
    transport.create_error = TimeoutError("lost reply")
    transport.accept_before_error = accepted
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    run, calls, waves, _ = journal(run_id)
    assert run.state == "attention" and calls[0].state == "uncertain"
    assert waves[0].state == "submission_unknown"
    transport.create_error = None
    for _ in range(3):
        service.sync(run_id)
        service.submit_pending(run_id)
    assert transport.created == ["batch-1"]
    assert service.outstanding(run_id)
    if accepted:
        complete(transport, "batch-1")
        service.sync(run_id)
        assert journal(run_id)[1][0].state == "completed"
    else:
        assert journal(run_id)[0].state == "attention"


def test_terminal_missing_and_invalid_results_preserve_successful_calls():
    run_id = seed(3)
    transport = FakeTransport()
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    ids = [request["custom_id"] for request in transport.requests("batch-1")]
    complete(transport, "batch-1", status="expired", missing=[ids[1]], invalid=[ids[2]])
    service.sync(run_id)
    _, calls, waves, usages = journal(run_id)
    assert [call.state for call in calls] == ["completed", "failed", "failed"]
    assert usages == 2 and len(waves[0].consumed_ids) == 3
    service.submit_pending(run_id)
    assert len(transport.created) == 1


def test_failed_call_explicit_retry_sends_only_that_call():
    run_id = seed(2)
    transport = FakeTransport()
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    failed_id = transport.requests("batch-1")[1]["custom_id"]
    complete(transport, "batch-1", failed=[failed_id])
    service.sync(run_id)
    with write_session_scope() as session:
        call = session.scalar(select(GenerationCall).where(GenerationCall.state == "failed"))
        call.state = "prepared"
        call.wave_id = None
    service.submit_pending(run_id)
    assert len(transport.requests("batch-2")) == 1
    complete(transport, "batch-2")
    service.sync(run_id)
    assert all(call.state == "completed" for call in journal(run_id)[1])
    assert journal(run_id)[3] == 3


def test_cancellation_keeps_received_successes_and_stops_new_calls():
    run_id = seed(2)
    transport = FakeTransport()
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    with write_session_scope() as session:
        session.get(GenerationRun, run_id).cancel_requested = True
    service.sync(run_id, cancelling=True)
    assert transport.cancelled == ["batch-1"]
    ids = [request["custom_id"] for request in transport.requests("batch-1")]
    complete(transport, "batch-1", status="cancelled", missing=[ids[1]])
    service.sync(run_id, cancelling=True)
    service.submit_pending(run_id)
    assert [call.state for call in journal(run_id)[1]] == ["completed", "failed"]
    assert transport.created == ["batch-1"] and not service.outstanding(run_id)


def test_import_transaction_rolls_back_response_usage_and_consumed_ids(monkeypatch):
    run_id = seed(2)
    transport = FakeTransport()
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    complete(transport, "batch-1")
    original = service._import_result
    imported = []

    def fail_second(*args):
        imported.append(1)
        original(*args)
        if len(imported) == 2:
            raise OSError("disk unavailable")

    monkeypatch.setattr(service, "_import_result", fail_second)
    with pytest.raises(OSError, match="disk unavailable"):
        service.sync(run_id)
    _, calls, waves, usages = journal(run_id)
    assert usages == 0 and waves[0].consumed_ids == []
    assert all(call.response_data is None for call in calls)
    monkeypatch.setattr(service, "_import_result", original)
    service.sync(run_id)
    assert journal(run_id)[3] == 2


def test_foreign_output_id_never_imports_responses_or_usage():
    run_id = seed()
    transport = FakeTransport()
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    complete(transport, "batch-1")
    transport.outputs["output-batch-1"]["foreign"] = BatchResult(
        custom_id="foreign", content='{"issues": []}', prompt_tokens=10,
    )
    with pytest.raises(ValueError, match="étrangers"):
        service.sync(run_id)
    assert journal(run_id)[3] == 0 and journal(run_id)[1][0].response_data is None


def test_denied_upload_blocks_automatic_polling_and_can_cancel_prepared():
    class AccessError(Exception):
        status_code = 403

    run_id = seed()
    transport = FakeTransport()
    transport.upload_error = AccessError("denied")
    service = GenerationBatchTransport(transport=transport)
    with pytest.raises(AccessError):
        service.submit_pending(run_id)
    assert journal(run_id)[0].state == "blocked"
    service.sync(run_id)
    service.submit_pending(run_id)
    assert transport.upload_attempts == 1 and not transport.created
    service.sync(run_id, cancelling=True)
    assert not service.outstanding(run_id)
    assert journal(run_id)[1][0].state == "prepared"


def test_journal_lock_is_reentrant_and_scope_can_nest_global_transport():
    with generation_operation("run-1"), generation_operation(), generation_operation("run-1"):
        pass


def test_native_claude_unknown_submission_waits_for_results_and_imports_once():
    run_id = seed(provider="anthropic")
    native = NativeClaudeClient()
    native.create_error = TimeoutError("reply lost after acceptance")
    service = GenerationBatchTransport(transport=AnthropicBatchTransport(native))
    service.submit_pending(run_id)
    service.sync(run_id)
    service.sync(run_id)
    assert native.created == ["msgbatch-1"] and journal(run_id)[0].state == "attention"
    native.create_error = None
    wire_id = native.inputs["msgbatch-1"][0]["custom_id"].partition("_")[2]
    native.complete("msgbatch-1", answers={wire_id: DocumentTextReview(issues=[])})
    service.sync(run_id)
    service.sync(run_id)
    assert journal(run_id)[1][0].response_data == {"issues": []}
    assert journal(run_id)[3] == 1 and native.created == ["msgbatch-1"]
    with session_scope() as session:
        usage = session.scalar(select(LLMUsage))
        assert usage.provider == "anthropic" and usage.cache_write_prompt_tokens == 10


def test_known_rejected_submission_requires_explicit_call_retry():
    class RejectedError(Exception):
        status_code = 422

    run_id = seed()
    transport = FakeTransport()
    transport.create_error = RejectedError("invalid request")
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    service.sync(run_id)
    service.submit_pending(run_id)
    assert transport.created == ["batch-1"] and journal(run_id)[1][0].state == "failed"
    assert not service.outstanding(run_id) and journal(run_id)[3] == 0


def test_saved_provider_routes_are_used_when_configuration_changes(monkeypatch):
    run_id = seed(provider="anthropic")
    transport = FakeTransport()
    selected = []

    def factory(provider, *, settings):
        selected.append(provider)
        return transport

    monkeypatch.setattr("smartapply.pipeline.generation.transport.get_batch_transport", factory)
    service = GenerationBatchTransport()
    service.submit_pending(run_id)
    assert selected == ["anthropic"]
    body = transport.requests("batch-1")[0]["body"]
    assert body["model"] == "claude-haiku-5-5" and body["max_completion_tokens"] == 1234
    complete(transport, "batch-1")
    GenerationBatchTransport().sync(run_id)
    assert selected == ["anthropic", "anthropic"]


@pytest.mark.parametrize("moment", ["before_upload", "during_upload"])
def test_changed_offer_is_pruned_before_paid_submission(moment):
    run_id = seed()
    transport = FakeTransport()
    original_upload = transport.upload

    def change_offer():
        with write_session_scope() as session:
            session.scalar(select(Job)).description = "Changed source body"

    def upload_and_change(content):
        file_id = original_upload(content)
        change_offer()
        return file_id

    if moment == "before_upload":
        change_offer()
    else:
        transport.upload = upload_and_change
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id)
    _, calls, waves, usages = journal(run_id)
    assert not transport.created and usages == 0
    assert calls[0].state == "failed" and waves[0].requests == []
    with session_scope() as session:
        assert session.scalar(select(GenerationItem)).state == "stale"


def test_new_pending_duplicate_prevents_batch_submission():
    from smartapply.database.models import JobDuplicateStatus

    run_id = seed()
    with write_session_scope() as session:
        job = session.scalar(select(Job))
        session.add(Job(
            external_id="pending-alias", title="AI Engineer", source_company="Acme",
            source="manual", description="Alias", possible_duplicate_of_id=job.id,
            duplicate_review_status=JobDuplicateStatus.PENDING,
        ))
    transport = FakeTransport()
    GenerationBatchTransport(transport=transport).submit_pending(run_id)
    assert not transport.created and journal(run_id)[1][0].state == "failed"


@pytest.mark.parametrize("path", ["submit_pending", "sync"])
def test_worker_stop_cancels_prepared_waves_before_upload(path):
    run_id = seed()
    transport = FakeTransport()
    service = GenerationBatchTransport(transport=transport)
    if path == "sync":
        # Leave the durable wave prepared, as after a process interruption.
        transport.upload_error = TimeoutError("Upload interrupted")
        with pytest.raises(TimeoutError):
            service.submit_pending(run_id)
        transport.upload_error = None
    attempts = transport.upload_attempts
    getattr(service, path)(run_id, stop_requested=lambda: True)
    run, calls, waves, usages = journal(run_id)
    assert run.cancel_requested and not transport.created and usages == 0
    assert transport.upload_attempts == attempts
    assert calls[0].state == "prepared"
    if path == "sync":
        assert waves[0].state == "cancelled"


def test_worker_stop_during_upload_never_submits_and_retains_request():
    run_id = seed()
    transport = FakeTransport()
    stopped = Event()
    original = transport.upload

    def upload(content):
        file_id = original(content)
        stopped.set()
        return file_id

    transport.upload = upload
    service = GenerationBatchTransport(transport=transport)
    service.submit_pending(run_id, stop_requested=stopped.is_set)
    run, calls, waves, usages = journal(run_id)
    assert run.cancel_requested and not transport.created and usages == 0
    assert waves[0].state == "cancelled"
    assert calls[0].state == "prepared" and calls[0].wave_id is None
