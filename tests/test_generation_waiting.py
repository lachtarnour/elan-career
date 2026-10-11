"""Unchanged native Batch waits do not replay completed document stages."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import event, select

from smartapply.database import get_engine, session_scope, write_session_scope
from smartapply.database.generation_runs import GenerationCall, GenerationItem, GenerationRun
from smartapply.database.models import (
    Application,
    GeneratedDocument,
    Job,
    JobDuplicateStatus,
    JobStatus,
)
from smartapply.database.session import subscribe_database_changes
from smartapply.llm.batch import BatchSnapshot
from smartapply.pipeline import Pipeline
from smartapply.pipeline.generation.coordinator import GenerationCoordinator
from smartapply.pipeline.generation.replay import ReplayProvider
from tests import test_generation_batch_pipeline as batch_tests
from tests.test_batch_analysis import FakeTransport
from tests.test_generation_batch_pipeline import Harness, journal, native_routes


@pytest.fixture
def analyzed_jobs(isolated_db):
    return batch_tests.analyzed_jobs.__wrapped__(isolated_db)


def _count_replays(monkeypatch):
    counts = Counter()
    original = ReplayProvider.complete_json

    def complete(provider, **kwargs):
        counts[kwargs["purpose"]] += 1
        return original(provider, **kwargs)

    def factory(**kwargs):
        counts["pipeline"] += 1
        return Pipeline(**kwargs)

    monkeypatch.setattr(ReplayProvider, "complete_json", complete)
    return counts, factory


@pytest.mark.parametrize("stage", ["cv_adaptation", "application_texts", "document_review"])
def test_unchanged_wait_survives_new_coordinators_without_replay_then_advances_same_poll(
    analyzed_jobs, monkeypatch, tmp_path, stage,
):
    ids = analyzed_jobs(2)
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    run_id = harness.coordinator().start(ids, mode="batch")
    stages = ["cv_adaptation", "application_texts", "document_review"]
    for _ in stages[:stages.index(stage)]:
        harness.complete(run_id)
        harness.coordinator().sync()
    before = journal(run_id)
    saved = {call.id: (deepcopy(call.request_body), deepcopy(call.response_data), call.state)
             for call in before[2]}
    app_ids = [item.application_id for item in before[1]]
    created = list(harness.openai.created)
    counts, factory = _count_replays(monkeypatch)
    sql = Counter()
    notifications = []
    unsubscribe = subscribe_database_changes(notifications.append, with_topics=True)

    def statements(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            sql["select"] += 1
            if "generation_runs.profile_data" in statement:
                sql["run_profile"] += 1
            if "generation_item_contexts.profile_data" in statement:
                sql["item_profile"] += 1
            if "generation_waves.requests" in statement:
                sql["wave_requests"] += 1
        elif statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            sql["write"] += 1

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", statements)
    try:
        for _ in range(3):
            sql.clear()
            GenerationCoordinator(provider=harness.provider, pipeline_factory=factory).sync()
            assert not counts
            # Transport and finish metadata remain bounded independently of
            # how many completed stages precede the waiting checkpoint.
            assert sql["run_profile"] == sql["item_profile"] == 0
            assert sql["wave_requests"] == 1
            assert sql["write"] == 0 and notifications == []
            assert sql["select"] < 110  # Previously 114/130/146 for these two offers.
    finally:
        event.remove(engine, "before_cursor_execute", statements)
        unsubscribe()
    assert harness.openai.created == created
    after = journal(run_id)
    assert after[0].updated_at == before[0].updated_at
    assert saved == {call.id: (call.request_body, call.response_data, call.state) for call in after[2]}
    assert [item.application_id for item in after[1]] == app_ids
    harness.complete(run_id)
    GenerationCoordinator(provider=harness.provider, pipeline_factory=factory).sync()
    assert counts["pipeline"] == 2 and counts[stage] == 2
    assert [item.application_id for item in journal(run_id)[1]] == app_ids
    if stage == "document_review":
        assert journal(run_id)[0].state == "completed"
    else:
        assert len(harness.openai.created) == len(created) + 1
        assert [call.purpose for call in journal(run_id)[2]][-2:] == [stages[stages.index(stage) + 1]] * 2


@pytest.mark.parametrize("state", ["submitted", "submitting", "uncertain"])
def test_unresolved_journal_state_never_replays_or_resends(
    analyzed_jobs, monkeypatch, tmp_path, state,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    coordinator = harness.coordinator()
    run_id = coordinator.start(ids, mode="batch")
    with write_session_scope() as session:
        session.scalar(select(GenerationCall)).state = state
    # A provider recovery may temporarily leave the durable state untouched.
    monkeypatch.setattr(coordinator.transport, "sync", lambda *_args, **_kwargs: None)
    counts, factory = _count_replays(monkeypatch)
    coordinator.pipeline_factory = factory
    coordinator.sync()
    assert not counts and harness.openai.created == ["batch-1"]
    assert journal(run_id)[1][0].state == "waiting"
    assert journal(run_id)[0].state == ("active" if state == "submitted" else "attention")


@pytest.mark.parametrize("change", [
    "source", "analysis", "archive", "pending_duplicate", "cv", "document", "docx", "pdf",
    "application_status", "submitted", "alias_application",
])
def test_waiting_skip_keeps_source_analysis_and_protected_application_guards(
    analyzed_jobs, monkeypatch, tmp_path, change,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    run_id = harness.coordinator().start(ids, mode="batch")
    _, items, calls, _ = journal(run_id)
    app_id = items[0].application_id
    request = deepcopy(calls[0].request_body)
    with write_session_scope() as session:
        job = session.get(Job, ids[0])
        app = session.get(Application, app_id)
        if change == "source":
            job.description += " Modified while Batch was waiting."
        elif change == "analysis":
            job.analysis.role_type = "Changed role"
        elif change == "archive":
            job.status = JobStatus.ARCHIVED
        elif change == "pending_duplicate":
            session.add(Job(
                external_id="pending", source="manual", title="Possible duplicate",
                source_company="Acme", description="Python", possible_duplicate_of_id=job.id,
                duplicate_review_status=JobDuplicateStatus.PENDING,
            ))
        elif change == "cv":
            app.cv_json = {"preserved": "existing candidate work"}
        elif change == "document":
            app.documents.append(GeneratedDocument(doc_type="letter", content="Preserved work"))
        elif change in {"docx", "pdf"}:
            setattr(app, "cv_" + change + "_path", "/tmp/preserved." + change)
        elif change == "application_status":
            app.status = JobStatus.ARCHIVED
        elif change == "submitted":
            app.form_submitted_at = journal(run_id)[0].created_at
        elif change == "alias_application":
            alias = Job(
                external_id="confirmed", source="manual", title="Confirmed alias",
                source_company="Acme", description="Python", canonical_job_id=job.id,
                duplicate_review_status=JobDuplicateStatus.CONFIRMED,
            )
            alias.application = Application(status=JobStatus.SENT, cv_json={"preserved": "sent"})
            session.add(alias)
    counts, factory = _count_replays(monkeypatch)
    GenerationCoordinator(provider=harness.provider, pipeline_factory=factory).sync()
    assert not counts
    assert journal(run_id)[1][0].state == "stale"
    assert journal(run_id)[2][0].request_body == request
    assert harness.openai.created == ["batch-1"]
    with session_scope() as session:
        app = session.get(Application, app_id)
        if change == "cv":
            assert app.cv_json == {"preserved": "existing candidate work"}
        elif change == "document":
            assert app.documents[0].content == "Preserved work"
        elif change in {"docx", "pdf"}:
            assert getattr(app, "cv_" + change + "_path") == "/tmp/preserved." + change
        elif change == "application_status":
            assert app.status == JobStatus.ARCHIVED
        elif change == "submitted":
            assert app.form_submitted_at is not None
        elif change == "alias_application":
            assert session.scalar(select(Application).where(Application.status == JobStatus.SENT)).cv_json


def test_waiting_notes_and_reservation_ack_gap_remain_recoverable(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    run_id = harness.coordinator().start(ids, mode="batch")
    item = journal(run_id)[1][0]
    with write_session_scope() as session:
        session.get(Application, item.application_id).notes = "Independent note retained"
    counts, factory = _count_replays(monkeypatch)
    GenerationCoordinator(provider=harness.provider, pipeline_factory=factory).sync()
    assert not counts
    with write_session_scope() as session:
        session.get(GenerationItem, item.id).application_id = None
    GenerationCoordinator(provider=harness.provider, pipeline_factory=factory).sync()
    assert counts["pipeline"] == 1
    assert journal(run_id)[1][0].application_id == item.application_id
    assert harness.openai.created == ["batch-1"]
    with session_scope() as session:
        assert session.get(Application, item.application_id).notes == "Independent note retained"


def test_waiting_owner_conflict_cannot_enter_another_durable_claim(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    run_id = harness.coordinator().start(ids, mode="batch")
    item = journal(run_id)[1][0]
    with write_session_scope() as session:
        session.add(GenerationRun(
            id="other-owner", mode="batch", state="paused", profile_data={}, routes={},
            reference_date="2026-10-11",
        ))
        session.flush()
        session.add(GenerationItem(
            run_id="other-owner", job_id=item.job_id, application_id=item.application_id,
            job_snapshot=item.job_snapshot, analysis_snapshot=item.analysis_snapshot,
            title=item.title, company=item.company, state="waiting",
        ))
    counts, factory = _count_replays(monkeypatch)
    GenerationCoordinator(provider=harness.provider, pipeline_factory=factory).sync()
    assert not counts and journal(run_id)[1][0].state == "failed"
    assert harness.openai.created == ["batch-1"]
    with session_scope() as session:
        assert session.get(GenerationRun, "other-owner").state == "paused"
        assert session.get(Application, item.application_id) is not None


def test_prepared_wait_still_replays_validates_and_submits_once(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    stopped = Event()
    transport = FakeTransport()
    upload = transport.upload

    def stop_upload(content):
        file_id = upload(content)
        stopped.set()
        return file_id

    transport.upload = stop_upload
    run_id = GenerationCoordinator(transport=transport).start(
        ids, mode="batch", stop_requested=stopped.is_set,
    )
    assert journal(run_id)[2][0].state == "prepared"
    with write_session_scope() as session:
        run = session.get(GenerationRun, run_id)
        run.state, run.cancel_requested = "active", False
    counts, factory = _count_replays(monkeypatch)
    resumed = FakeTransport()
    GenerationCoordinator(transport=resumed, pipeline_factory=factory).sync()
    assert counts["pipeline"] == counts["cv_adaptation"] == 1
    assert resumed.created == ["batch-1"] and journal(run_id)[2][0].state == "submitted"


def test_terminal_missing_result_fails_wait_in_same_followup_without_resend(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    run_id = harness.coordinator().start(ids, mode="batch")
    custom_id = journal(run_id)[3][0].requests[0]["custom_id"]
    harness.complete(run_id, status="expired", missing=[custom_id])
    counts, factory = _count_replays(monkeypatch)
    GenerationCoordinator(provider=harness.provider, pipeline_factory=factory).sync()
    assert counts["pipeline"] == counts["cv_adaptation"] == 1
    run, items, calls, waves = journal(run_id)
    assert run.state == items[0].state == calls[0].state == "failed"
    assert waves[0].state == "failed" and harness.openai.created == ["batch-1"]


def test_provider_progress_cancel_and_partial_terminal_import_still_notify(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs(2)
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    coordinator = harness.coordinator()
    run_id = coordinator.start(ids, mode="batch")
    before = journal(run_id)
    notifications = []
    unsubscribe = subscribe_database_changes(notifications.append, with_topics=True)
    try:
        harness.openai.snapshots["batch-1"] = replace(harness.openai.snapshots["batch-1"], completed=1)
        coordinator.sync()
        progress = journal(run_id)
        assert progress[0].updated_at > before[0].updated_at
        assert progress[3][0].provider_data["completed"] == 1
        assert any("generation_waves" in topics for topics in notifications)
        notifications.clear()
        coordinator.sync()
        assert notifications == [] and journal(run_id)[0].updated_at == progress[0].updated_at
        coordinator.cancel(run_id)
        assert any("generation_runs" in topics for topics in notifications)
        assert harness.openai.cancelled == ["batch-1"]
        cancelled = journal(run_id)
        assert cancelled[0].cancel_requested and cancelled[3][0].provider_data["status"] == "cancelling"
        notifications.clear()
        coordinator.sync()
        assert notifications == [] and journal(run_id)[0].updated_at == cancelled[0].updated_at
        missing = cancelled[3][0].requests[1]["custom_id"]
        harness.complete(run_id, status="cancelled", missing=[missing])
        coordinator.sync()
        run, items, calls, waves = journal(run_id)
        assert run.state == "paused" and run.cancel_requested
        assert [call.state for call in calls] == ["completed", "failed"]
        assert calls[0].response_data and len(waves[0].consumed_ids) == 2
        assert [item.application_id for item in items] == [item.application_id for item in before[1]]
        assert any("generation_calls" in topics for topics in notifications)
        assert harness.openai.created == ["batch-1"]
    finally:
        unsubscribe()


def test_empty_pending_query_still_submits_previously_bound_prepared_wave(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    coordinator = harness.coordinator()
    submit = coordinator.transport._submit
    monkeypatch.setattr(coordinator.transport, "_submit", lambda *_args, **_kwargs: False)
    run_id = coordinator.start(ids, mode="batch")
    _, _, calls, waves = journal(run_id)
    assert calls[0].state == waves[0].state == "prepared" and calls[0].wave_id == waves[0].id
    assert harness.openai.created == []
    monkeypatch.setattr(coordinator.transport, "_submit", submit)
    coordinator.transport.submit_pending(run_id)
    assert harness.openai.created == ["batch-1"]
    assert journal(run_id)[2][0].state == journal(run_id)[3][0].state == "submitted"


def test_stale_wait_with_retained_notes_is_released_once_without_poll_writes(
    analyzed_jobs, monkeypatch, tmp_path,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    coordinator = harness.coordinator()
    run_id = coordinator.start(ids, mode="batch")
    app_id = journal(run_id)[1][0].application_id
    with write_session_scope() as session:
        session.get(Application, app_id).notes = "Retain independent notes"
        session.get(Job, ids[0]).description += " Source changed."
    coordinator.sync()
    stale_run = journal(run_id)[0]
    with session_scope() as session:
        released_at = session.get(Application, app_id).updated_at
    notifications = []
    unsubscribe = subscribe_database_changes(notifications.append, with_topics=True)
    try:
        coordinator.sync()
        assert notifications == []
        assert journal(run_id)[0].updated_at == stale_run.updated_at
        with session_scope() as session:
            app = session.get(Application, app_id)
            assert app.updated_at == released_at and app.notes == "Retain independent notes"
    finally:
        unsubscribe()


@pytest.mark.parametrize("matches", [0, 2])
def test_unchanged_recovery_keeps_diagnostic_without_writes_then_recovers_once(
    analyzed_jobs, monkeypatch, tmp_path, matches,
):
    ids = analyzed_jobs()
    native_routes(monkeypatch, tmp_path)
    harness = Harness(monkeypatch)
    harness.openai.create_error = TimeoutError("Acknowledgement not available")
    harness.openai.accept_before_error = bool(matches)
    run_id = harness.coordinator().start(ids, mode="batch")
    if matches:
        harness.openai.snapshots["duplicate"] = replace(harness.openai.snapshots["batch-1"], id="duplicate")
    harness.coordinator().sync()
    attention = journal(run_id)
    expected = "aucun lot retrouvé" if not matches else "Plusieurs lots"
    assert expected in attention[0].last_error
    notifications, writes = [], []
    unsubscribe = subscribe_database_changes(notifications.append, with_topics=True)

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for _ in range(3):
            harness.coordinator().sync()
            assert notifications == writes == []
            run = journal(run_id)[0]
            assert run.last_error == attention[0].last_error and run.updated_at == attention[0].updated_at
        if matches:
            del harness.openai.snapshots["duplicate"]
        else:
            wave = attention[3][0]
            harness.openai.snapshots["batch-1"] = BatchSnapshot(
                id="batch-1", status="in_progress", input_file_id=wave.input_file_id,
                total=1, metadata={"elan_run_id": run_id, "elan_wave_id": wave.id},
            )
        harness.coordinator().sync()
        recovered = journal(run_id)
        assert recovered[0].state == "active" and recovered[0].last_error == ""
        assert recovered[2][0].state == "submitted" and recovered[2][0].attempt == 1
        assert recovered[2][0].request_body == attention[2][0].request_body
        assert notifications and writes and harness.openai.created == ["batch-1"]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        unsubscribe()
    harness.openai.create_error = None
    harness.finish(run_id)
    assert harness.submitted == Counter(cv_adaptation=1, application_texts=1, document_review=1)
