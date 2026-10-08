"""Analysis progress, service routing and Qt monitoring without network access."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from sqlalchemy import event

from smartapply.concurrency import TaskConflictError
from smartapply.database import session_scope
from smartapply.database.analysis_batches import (
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.models import (
    Application,
    Job,
    JobAnalysis,
    JobDuplicateStatus,
    JobScore,
    JobStatus,
)
from smartapply.database.session import get_engine
from smartapply.desktop.analysis_services import AnalysisServiceMixin
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.services import DesktopService
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.pipeline.batch_analysis.presentation import analysis_workspace
from smartapply.pipeline.errors import TaskCancelledError


class ControlledPool:
    def __init__(self):
        self.pending = []

    def start(self, worker):
        self.pending.append(worker)

    def finish_next(self):
        self.pending.pop(0).run()


def workspace(*, active=False, blocked=False, completed=False, failed=0, skipped=0):
    return {
        "candidates": [],
        "candidates_count": 0,
        "active_run_id": "run-1" if active or blocked else "",
        "runs": [
            {
                "id": "run-1",
                "is_active": active,
                "status": "blocked" if blocked else "in_progress" if active else "completed",
                "analyzed": 3 if completed else 0,
                "failed": failed,
                "skipped": skipped,
            }
        ],
    }


@pytest.fixture
def make_bridge(monkeypatch):
    application = QCoreApplication.instance() or QCoreApplication([])
    for name in (
        "refreshDashboard",
        "refreshShortlist",
        "refreshProfile",
        "refreshDiagnosticsIfStale",
    ):
        monkeypatch.setattr(DesktopBridge, name, lambda self: None)
    bridges = []

    def make(*, startup_sync=None):
        service = SimpleNamespace(
            initialize=lambda: None,
            analysis_top_k=lambda: 25,
            shortlist_top_k=lambda: 25,
            startup_maintenance_needed=lambda: False,
            analysis_workspace=lambda: workspace(),
            start_analysis_batch=lambda ids: workspace(active=True),
            start_immediate_analysis=lambda ids, **kwargs: {
                "workspace": workspace(),
                "report": SimpleNamespace(analyzed=len(ids), errors=[], cancelled=False),
            },
            retry_analysis_batch=lambda run_id: workspace(active=True),
            cancel_analysis_batch=lambda run_id: workspace(active=True),
        )
        if startup_sync is not None:
            service.sync_analysis_batches = startup_sync
        value = DesktopBridge(service)
        value._thread_pool = ControlledPool()
        value._mutation_pool = ControlledPool()
        value._read_pool = ControlledPool()
        value._analysis_controller._pool = ControlledPool()
        value._analysis_controller._monitor_pool = ControlledPool()
        bridges.append(value)
        return value

    yield make
    for value in bridges:
        value._shutdown_requested = True
        value._analysis_controller.stop()
        value._workflow_refresh_timer.stop()
        value._unsubscribe_resources()
        value._unsubscribe_database()
        for pool in (
            value._analysis_controller._pool,
            value._analysis_controller._monitor_pool,
            value._read_pool,
            value._mutation_pool,
            value._thread_pool,
        ):
            while pool.pending:
                pool.finish_next()
    application.processEvents()


def test_analysis_preview_reads_only_latest_offer_without_changing_offers(make_bridge):
    bridge = make_bridge()
    bridge._current_job = {"id": 42, "application": {"notes": "Unsaved offer context"}}
    original = deepcopy(bridge.currentJob)
    calls = []
    bridge.service.get_job = lambda job_id: (
        calls.append(job_id)
        or {
            "id": job_id,
            "title": f"Offer {job_id}",
            "description": "Full offer body",
        }
    )
    bridge.selectAnalysisOffer(7)
    bridge.selectAnalysisOffer(8)
    bridge.selectAnalysisOffer(9)
    assert bridge.analysisOfferLoading
    assert len(bridge._read_pool.pending) == 1
    bridge._read_pool.finish_next()
    assert bridge.analysisOffer == {}  # Result for offer 7 is obsolete.
    assert bridge.analysisOfferLoading
    bridge._read_pool.finish_next()
    assert calls == [7, 9]
    assert bridge.analysisOffer["id"] == 9 and not bridge.analysisOfferLoading
    assert bridge.currentJob == original
    assert not bridge.busy
    bridge._analysis_controller._loaded(workspace(active=True))
    assert bridge.analysisOffer["id"] == 9 and not bridge.analysisOfferLoading
    bridge._read_pool.finish_next()
    assert bridge.currentJob == original


def test_analysis_preview_handles_missing_offer_and_shutdown(make_bridge):
    bridge = make_bridge()
    bridge.service.get_job = lambda job_id: None
    bridge.selectAnalysisOffer(77)
    bridge._read_pool.finish_next()
    assert bridge.analysisOfferId == 77
    assert bridge.analysisOffer == {} and not bridge.analysisOfferLoading
    bridge.selectAnalysisOffer(77)
    bridge._shutdown_requested = True
    bridge._read_pool.finish_next()
    assert bridge.analysisOffer == {}
    bridge.selectAnalysisOffer(88)
    assert bridge.analysisOfferId == 77


def test_analysis_archive_refreshes_its_workspace_without_resetting_offers(make_bridge):
    bridge = make_bridge()
    bridge._current_job = {"id": 42, "application": {"notes": "Unsaved draft"}}
    original = deepcopy(bridge.currentJob)
    calls, notifications, archived, states = [], [], [], []
    bridge.service.get_job = lambda job_id: {"id": job_id, "status": "archived"}
    bridge.service.archive_analysis_offers = lambda ids: calls.append(ids) or ids
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    bridge.analysisOffersArchived.connect(archived.append)
    bridge.analysisArchivingChanged.connect(lambda: states.append(bridge.analysisArchiving))
    bridge.selectAnalysisOffer(7)
    bridge._read_pool.finish_next()

    bridge.archiveAnalysisOffers([7, 8, 7, 0])
    bridge.archiveAnalysisOffers([9])  # No second operation while archiving.
    assert bridge.analysisArchiving and len(bridge._mutation_pool.pending) == 1
    bridge._mutation_pool.finish_next()
    assert calls == [[7, 8]] and archived == [[7, 8]]
    assert states == [True, False]
    bridge._analysis_controller._pool.finish_next()
    bridge._read_pool.finish_next()
    assert bridge.analysisOfferId == 7 and bridge.analysisOffer["status"] == "archived"
    assert bridge.currentJob == original
    assert not bridge.busy
    assert notifications == [("2 offres archivées", "", "success")]


def test_analysis_archive_failure_keeps_selection_and_can_be_retried(make_bridge):
    bridge = make_bridge()
    archived, failures = [], []
    bridge.analysisOffersArchived.connect(archived.append)
    bridge._task_error = lambda message, trace: failures.append(message)

    def fail(ids):
        raise ValueError("Offre réservée par une analyse")

    bridge.service.archive_analysis_offers = fail
    bridge.archiveAnalysisOffers([])
    assert not bridge._mutation_pool.pending
    bridge.archiveAnalysisOffers([7])
    bridge._mutation_pool.finish_next()
    assert not bridge.analysisArchiving and archived == []
    assert failures == ["Offre réservée par une analyse"]
    bridge.archiveAnalysisOffers([7])
    assert bridge.analysisArchiving and len(bridge._mutation_pool.pending) == 1


def test_analysis_worker_preserves_offer_browser_and_global_busy(make_bridge):
    bridge = make_bridge()
    bridge._selected_job_id = 42
    bridge._current_job = {"id": 42, "notes": "A local draft"}
    bridge._jobs_loaded([{"id": 42, "title": "Selected offer"}])
    bridge._job_search = "Python"
    original = deepcopy(bridge.currentJob)
    resets, busy_changes = [], []
    bridge._jobs_model.modelReset.connect(lambda: resets.append(True))
    bridge.busyChanged.connect(lambda: busy_changes.append(True))
    bridge.startAnalysisBatch([42])
    assert bridge.analysisWorkspaceLoading and not bridge.busy
    assert bridge.analysisWorkspaceOperation == "start"
    assert len(bridge._analysis_controller._pool.pending) == 1
    assert bridge._thread_pool.pending == [] and bridge._read_pool.pending == []
    bridge._analysis_controller._pool.finish_next()
    assert bridge.currentJob == original and bridge._selected_job_id == 42
    assert bridge._job_search == "Python"
    assert bridge._jobs_model.rowCount() == 1
    assert resets == [] and busy_changes == []
    assert not bridge.analysisWorkspaceLoading
    assert bridge.analysisWorkspaceOperation == ""


def test_immediate_analysis_uses_its_own_worker_and_reports_completion(make_bridge):
    bridge = make_bridge()
    notifications = []
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    bridge._refresh_workflow = lambda: None
    bridge.startImmediateAnalysis([42])
    assert bridge.analysisWorkspaceOperation == "immediate"
    assert not bridge.busy
    bridge.cancelImmediateAnalysis()
    assert bridge._analysis_controller._pool.pending[0].cancel_requested
    bridge._analysis_controller._pool.finish_next()
    assert bridge.analysisWorkspaceOperation == ""
    assert notifications == [("Analyse immédiate terminée", "1 offre analysée.", "success")]


def test_stop_during_batch_preparation_cancels_a_run_created_in_the_same_worker(make_bridge):
    bridge = make_bridge()
    calls = []

    def launch(_ids, *, stop_requested):
        assert stop_requested()
        result = workspace(active=True)
        result["started_run_id"] = "run-1"
        result["runs"][0]["can_cancel"] = True
        return result

    bridge.service.start_analysis_batch = launch
    bridge.service.cancel_analysis_batch = lambda run_id: calls.append(run_id) or workspace()
    bridge.startAnalysisBatch([42])
    bridge.cancelBatchStart()
    assert bridge._analysis_controller._pool.pending[0].cancel_requested
    bridge._analysis_controller._pool.finish_next()
    QCoreApplication.instance().processEvents()
    assert len(bridge._analysis_controller._pool.pending) == 1
    bridge._analysis_controller._pool.finish_next()
    assert calls == ["run-1"]


def test_startup_resumes_monitoring_without_opening_analysis_page(make_bridge):
    calls = []
    bridge = make_bridge(startup_sync=lambda: calls.append("sync") or workspace(active=True))
    assert bridge._active_route == "dashboard"
    QCoreApplication.instance().processEvents()
    assert len(bridge._analysis_controller._pool.pending) == 1
    bridge._analysis_controller._pool.finish_next()
    assert calls == ["sync"] and bridge.analysisWorkspace["active_run_id"] == "run-1"
    assert bridge._analysis_controller._timer.interval() == 120_000
    assert bridge._analysis_controller._timer.isActive()
    assert not bridge.busy


def test_two_minute_timer_stops_when_no_run_is_active(make_bridge):
    bridge = make_bridge()
    controller = bridge._analysis_controller
    controller._loaded(workspace(active=True))
    assert controller._timer.isActive()
    controller._loaded(workspace(completed=True))
    assert not controller._timer.isActive()


def test_denied_upload_stops_polling_without_reporting_a_completed_analysis(make_bridge):
    bridge = make_bridge()
    notifications = []
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    controller = bridge._analysis_controller
    controller._loaded(workspace(active=True))
    controller._loaded(workspace(blocked=True))
    assert not controller._timer.isActive()
    assert bridge.analysisWorkspace["active_run_id"] == "run-1"
    assert notifications == []


def test_timer_syncs_remote_progress_even_on_another_page(make_bridge):
    bridge = make_bridge()
    calls = []
    bridge.service.sync_analysis_batches = lambda: calls.append("sync") or workspace(active=True)
    bridge.setActiveRoute("duplicates")
    bridge._analysis_controller._loaded(workspace(active=True))
    bridge._analysis_controller._timer.timeout.emit()
    bridge._analysis_controller._pool.finish_next()
    assert calls == ["sync"] and bridge._active_route == "duplicates"


@pytest.mark.parametrize(
    "failed, skipped, kind", [(0, 0, "success"), (1, 0, "warning"), (0, 1, "warning")]
)
def test_completion_notification_is_emitted_once_per_transition(make_bridge, failed, skipped, kind):
    bridge = make_bridge()
    notifications = []
    refreshed = []
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    bridge._refresh_workflow = lambda: refreshed.append(True)
    controller = bridge._analysis_controller
    controller._loaded(workspace(active=True))
    controller._loaded(workspace(completed=True, failed=failed, skipped=skipped))
    controller._loaded(workspace(completed=True, failed=failed, skipped=skipped))
    assert len(notifications) == 1 and refreshed == [True]
    assert notifications[0][0] == "Analyse Batch terminée"
    assert "3 analyses enregistrées" in notifications[0][1]
    assert notifications[0][2] == kind


def test_transient_error_preserves_workspace_and_polling_until_recovery(make_bridge):
    bridge = make_bridge()
    controller = bridge._analysis_controller
    controller._loaded(workspace(active=True))
    before = deepcopy(bridge.analysisWorkspace)

    def disconnected():
        raise RuntimeError("Temporary provider disconnection")

    bridge.service.sync_analysis_batches = disconnected
    bridge.refreshAnalysisWorkspace()
    controller._pool.finish_next()
    assert bridge.analysisWorkspaceError == "Temporary provider disconnection"
    assert bridge.analysisWorkspace == before
    assert controller._timer.isActive() and not bridge.analysisWorkspaceLoading
    bridge.service.sync_analysis_batches = lambda: workspace(active=True)
    controller._timer.timeout.emit()
    controller._pool.finish_next()
    assert bridge.analysisWorkspaceError == "" and controller._timer.isActive()


def test_navigation_queues_one_local_read_behind_current_sync(make_bridge):
    bridge = make_bridge()
    calls = []
    bridge.service.sync_analysis_batches = lambda: calls.append("sync") or workspace(active=True)
    bridge.service.analysis_workspace = lambda: calls.append("read") or workspace(active=True)
    bridge.refreshAnalysisWorkspace()
    bridge.setActiveRoute("analysis")
    bridge.setActiveRoute("analysis")
    assert len(bridge._analysis_controller._pool.pending) == 1
    bridge._analysis_controller._pool.finish_next()
    QCoreApplication.instance().processEvents()
    assert len(bridge._analysis_controller._pool.pending) == 1
    bridge._analysis_controller._pool.finish_next()
    assert calls == ["sync", "read"]


@pytest.mark.parametrize("fails", [False, True])
def test_shutdown_stops_polling_and_waits_for_analysis_worker(make_bridge, fails):
    bridge = make_bridge()
    controller = bridge._analysis_controller
    controller._loaded(workspace(active=True))

    def sync():
        if fails:
            raise RuntimeError("Connection closed during shutdown")
        return workspace(completed=True)

    bridge.service.sync_analysis_batches = sync
    bridge.refreshAnalysisWorkspace()
    bridge.requestShutdown()
    assert bridge.shutdownRequested and not bridge.shutdownReady
    assert not controller._timer.isActive()
    controller._pool.finish_next()
    assert bridge.shutdownReady
    assert not controller._timer.isActive()
    assert bridge.analysisWorkspace["active_run_id"] == "run-1"  # Late results are ignored.


def test_bridge_slots_forward_normalized_ids_and_execution_identifiers(make_bridge):
    bridge = make_bridge()
    calls = []
    bridge.service.start_analysis_batch = lambda ids, **kwargs: (
        calls.append(("start", ids)) or workspace(active=True)
    )
    bridge.service.retry_analysis_batch = lambda run_id: (
        calls.append(("retry", run_id)) or workspace(active=True)
    )
    bridge.service.cancel_analysis_batch = lambda run_id: (
        calls.append(("cancel", run_id)) or workspace(active=True)
    )
    bridge.startAnalysisBatch([42, "42", "bad", -1, 7])
    bridge._analysis_controller._pool.finish_next()
    bridge.retryAnalysisBatch("run-42")
    bridge._analysis_controller._pool.finish_next()
    bridge.cancelAnalysisBatch("run-42")
    bridge._analysis_controller._pool.finish_next()
    assert calls == [("start", [42, 7]), ("retry", "run-42"), ("cancel", "run-42")]


def _job(external_id, **changes):
    values = {
        "external_id": external_id,
        "title": "Machine Learning Engineer",
        "company": "Example AI",
        "description": "Large body " * 1000,
        "source": "manual",
        "location": "Paris",
        "filtered_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "score": JobScore(
            components={
                "filter_disposition": "pending_relevance",
                "hard_reject": HardRejectResult(decisions=()).audit_payload(),
            }
        ),
    }
    values.update(changes)
    return Job(**values)


def _run_items(session, jobs, *, state="active", item_states=None, error="", cancel=False):
    run = AnalysisBatchRun(
        id="run-test",
        state=state,
        model="gpt-5.4-mini",
        profile_data={"large_profile": "x" * 50_000},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 3, tzinfo=timezone.utc) if state == "partial" else None,
        last_error=error,
        cancel_requested=cancel,
    )
    session.add(run)
    session.flush()
    items = []
    for index, job in enumerate(jobs):
        item = AnalysisBatchItem(
            run_id=run.id,
            job_id=job.id,
            job_snapshot="snapshot",
            title=job.title,
            company=job.company,
            state=item_states[index] if item_states else "extraction_pending",
            analyzer_input={"large_offer": "x" * 50_000},
            offer_data={"large_extraction": "x" * 50_000},
            error=f"Problem {index}" if item_states and item_states[index] != "completed" else "",
        )
        session.add(item)
        items.append(item)
    return run, items


def test_workspace_counts_exclude_aliases_duplicates_history_and_reserved_offers(isolated_db):
    with session_scope() as session:
        available = _job("available")
        reserved = _job("reserved")
        rejected = _job("rejected")
        proof = HardRejectResult(decisions=()).audit_payload()
        proof["rejected"] = True
        rejected.score.components = {"filter_disposition": "rejected", "hard_reject": proof}
        archived = _job("archived", archived_at=datetime.now(timezone.utc))
        pending = _job("pending", duplicate_review_status=JobDuplicateStatus.PENDING)
        sent = _job("sent", status=JobStatus.SENT)
        dossier = _job("dossier", application=Application(status="draft"))
        analyzed = _job("analyzed", analysis=JobAnalysis(fit_score=0.7))
        session.add_all([available, reserved, rejected, archived, pending, sent, dossier, analyzed])
        session.flush()
        alias = _job("alias", canonical_job_id=available.id)
        session.add(alias)
        _run_items(session, [reserved])
        available_id = available.id
    data = analysis_workspace()
    assert data["candidates_count"] == data["pending_count"] == 1
    assert [job["id"] for job in data["candidates"]] == [available_id]
    assert data["active_run_id"] == "run-test"


def test_archive_analysis_selection_preserves_history_and_unselected_offers(isolated_db):
    with session_scope() as session:
        first = _job("archive-first", analysis=JobAnalysis(fit_score=0.7),
                     analyzed_at=datetime.now(timezone.utc))
        second = _job("archive-second")
        other = _job("archive-unselected", application=Application(notes="Keep draft"))
        session.add_all([first, second, other])
        session.flush()
        alias = _job("archive-alias", canonical_job_id=first.id)
        session.add(alias)
        run, items = _run_items(session, [first], state="completed", item_states=["completed"])
        session.flush()
        first_id, second_id, other_id, alias_id = first.id, second.id, other.id, alias.id
        run_id, item_id, analysis_id = run.id, items[0].id, first.analysis.id
        analyzed_at = first.analyzed_at

    service = DesktopService()
    assert service.archive_analysis_offers([first_id, second_id, first_id, 0, 999999]) == [first_id, second_id]
    assert service.archive_analysis_offers([first_id]) == []
    with session_scope() as session:
        first, second = session.get(Job, first_id), session.get(Job, second_id)
        assert first.status == second.status == JobStatus.ARCHIVED
        assert first.analysis.id == analysis_id and first.analysis.fit_score == 0.7
        assert first.analyzed_at.replace(tzinfo=timezone.utc) == analyzed_at
        assert first.score.components["rejection_reasons"] == ["manual_archive"]
        assert session.get(Job, alias_id).archived_at is None
        assert session.get(Job, other_id).archived_at is None
        assert session.get(Job, other_id).application.notes == "Keep draft"
        assert session.get(AnalysisBatchRun, run_id).state == "completed"
        assert session.get(AnalysisBatchItem, item_id).state == "completed"


@pytest.mark.parametrize("state", ["active", "attention", "blocked"])
def test_archive_analysis_selection_is_atomic_when_a_batch_reserves_an_offer(isolated_db, state):
    with session_scope() as session:
        free, reserved = _job("archive-free"), _job("archive-reserved")
        session.add_all([free, reserved])
        session.flush()
        _run_items(session, [reserved], state=state)
        ids = [free.id, reserved.id]
    with pytest.raises(TaskConflictError, match="analyse Batch"):
        DesktopService().archive_analysis_offers(ids)
    with session_scope() as session:
        assert all(session.get(Job, job_id).archived_at is None for job_id in ids)
        assert all("rejection_stage" not in session.get(Job, job_id).score.components for job_id in ids)


def test_archive_analysis_selection_rolls_back_on_storage_failure(isolated_db, monkeypatch):
    with session_scope() as session:
        jobs = [_job("archive-rollback-first"), _job("archive-rollback-second")]
        session.add_all(jobs)
        session.flush()
        ids = [job.id for job in jobs]
    original = DesktopService._archive_job_in_session

    def archive(session, job):
        if job.id == ids[1]:
            raise RuntimeError("Storage failure")
        original(session, job)

    monkeypatch.setattr(DesktopService, "_archive_job_in_session", staticmethod(archive))
    with pytest.raises(RuntimeError, match="Storage failure"):
        DesktopService().archive_analysis_offers(ids)
    with session_scope() as session:
        assert all(session.get(Job, job_id).archived_at is None for job_id in ids)
        assert all("rejection_stage" not in session.get(Job, job_id).score.components for job_id in ids)


def test_workspace_reports_active_phase_deadline_and_waiting_second_wave(isolated_db):
    with session_scope() as session:
        jobs = [_job("phase-first"), _job("phase-second")]
        session.add_all(jobs)
        session.flush()
        run, _ = _run_items(session, jobs)
        session.add(
            AnalysisBatchWave(
                id="wave-first",
                run_id=run.id,
                phase="extraction",
                state="in_progress",
                requests=[{"large_prompt": "x" * 50_000}],
                provider_data={
                    "status": "in_progress",
                    "total": 2,
                    "completed": 1,
                    "failed": 0,
                    "expires_at": 1767312000,
                },
            )
        )
    summary = analysis_workspace()["runs"][0]
    assert summary["phase"] == "extraction" and summary["status"] == "in_progress"
    assert summary["phase_label"].startswith("Étape 1/2")
    assert summary["can_cancel"] and not summary["can_retry"]
    assert summary["created_at"] == "2026-01-01T00:00:00+00:00"
    assert summary["expires_at"] == "2026-01-02T00:00:00+00:00"
    assert summary["phase_batches"][0]["completed"] == 1
    assert summary["phase_batches"][0]["batches"][0]["offers"] == 2
    assert summary["phase_batches"][1]["status"] == "waiting"


def test_workspace_keeps_each_run_and_provider_batch_progress_distinct(isolated_db):
    with session_scope() as session:
        jobs = [_job(f"multi-{index}") for index in range(3)]
        session.add_all(jobs)
        session.flush()
        first, _ = _run_items(session, jobs[:2])
        second = AnalysisBatchRun(
            id="run-second",
            state="active",
            model="test",
            profile_data={},
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        session.add(second)
        session.flush()
        session.add(
            AnalysisBatchItem(
                run_id=second.id,
                job_id=jobs[2].id,
                job_snapshot="snapshot",
                title=jobs[2].title,
                company=jobs[2].company,
                analyzer_input={},
                state="extraction_pending",
            )
        )
        session.add_all(
            [
                AnalysisBatchWave(
                    id="wave-a",
                    run_id=first.id,
                    phase="extraction",
                    state="imported",
                    provider_data={
                        "status": "completed",
                        "total": 1,
                        "offers": 1,
                        "completed": 1,
                        "failed": 0,
                    },
                ),
                AnalysisBatchWave(
                    id="wave-b",
                    run_id=first.id,
                    phase="extraction",
                    state="in_progress",
                    provider_data={
                        "status": "in_progress",
                        "total": 1,
                        "offers": 1,
                        "completed": 0,
                        "failed": 0,
                    },
                ),
                AnalysisBatchWave(
                    id="wave-c",
                    run_id=second.id,
                    phase="extraction",
                    state="prepared",
                    provider_data={"total": 1, "offers": 1},
                ),
            ]
        )
    data = analysis_workspace()
    assert data["active_run_count"] == 2
    assert data["active_run_id"] == "run-second"
    assert len(data["runs"]) == 2
    first_summary = next(run for run in data["runs"] if run["id"] == first.id)
    second_summary = next(run for run in data["runs"] if run["id"] == second.id)
    phase = first_summary["phase_batches"][0]
    assert phase["batch_count"] == 2 and phase["total"] == 2
    assert [
        (batch["number"], batch["offers"], batch["completed"]) for batch in phase["batches"]
    ] == [(1, 1, 1), (2, 1, 0)]
    assert second_summary["phase_batches"][0]["batch_count"] == 1
    assert second_summary["phase_batches"][0]["batches"][0]["status"] == "prepared"


def test_workspace_reports_blocked_upload_as_recoverable_not_waiting(isolated_db):
    with session_scope() as session:
        job = _job("blocked-upload")
        session.add(job)
        session.flush()
        run, _ = _run_items(session, [job], state="blocked")
        run.last_error = "Accès OpenAI requis : api.files.write"
        session.add(
            AnalysisBatchWave(
                id="wave-blocked",
                run_id=run.id,
                phase="extraction",
                state="prepared",
                requests=[{"custom_id": "request-1"}],
                provider_data={"total": 1},
            )
        )
    data = analysis_workspace()
    summary = data["runs"][0]
    assert data["active_run_id"] == summary["id"]
    assert data["candidates_count"] == 0
    assert summary["phase"] == "extraction" and summary["status"] == "blocked"
    assert summary["status_label"] == "Connexion à rétablir"
    assert [item["id"] for item in summary["items"]] == [job.id]
    assert summary["items"][0]["state_label"] == "À extraire"
    assert summary["can_retry"] and summary["can_cancel"]
    assert not summary["is_active"]
    assert summary["phase_batches"][0]["status"] == "prepared"
    assert summary["phase_batches"][0]["batches"][0]["offers"] == 1


def test_workspace_preserves_partial_failure_counts_retry_and_error_details(isolated_db):
    with session_scope() as session:
        jobs = [_job(f"partial-{index}") for index in range(3)]
        session.add_all(jobs)
        session.flush()
        run, _ = _run_items(
            session,
            jobs,
            state="partial",
            item_states=["completed", "extraction_failed", "skipped"],
        )
        session.add(
            AnalysisBatchWave(
                id="wave-first",
                run_id=run.id,
                phase="extraction",
                state="imported",
                provider_data={"status": "expired", "total": 3, "completed": 2, "failed": 0},
            )
        )
        session.add(
            AnalysisBatchWave(
                id="wave-second",
                run_id=run.id,
                phase="evaluation",
                state="imported",
                provider_data={"status": "completed", "total": 1, "completed": 1, "failed": 0},
            )
        )
    summary = analysis_workspace()["runs"][0]
    assert summary["phase"] == "done" and summary["status"] == "partial"
    assert summary["total"] == 3 and summary["analyzed"] == summary["completed"] == 1
    assert summary["failed"] == summary["skipped"] == 1
    assert summary["can_retry"] and not summary["can_cancel"]
    assert summary["completed_at"] == "2026-01-03T00:00:00+00:00"
    assert [item["message"] for item in summary["errors"]] == ["Problem 1", "Problem 2"]
    assert summary["phase_batches"][0]["status"] == "expired"


def test_workspace_read_does_not_fetch_stored_prompts_profile_or_offer_bodies(isolated_db):
    with session_scope() as session:
        job = _job("heavy-fields")
        session.add(job)
        session.flush()
        run, _ = _run_items(session, [job])
        session.add(
            AnalysisBatchWave(
                id="wave-heavy",
                run_id=run.id,
                phase="extraction",
                state="in_progress",
                requests=[{"large_prompt": "x" * 50_000}],
                provider_data={"status": "in_progress"},
            )
        )
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        analysis_workspace()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements
    for forbidden in (
        "profile_data",
        "analyzer_input",
        "offer_data",
        ".requests",
        ".description",
        "raw_response",
    ):
        assert all(forbidden not in statement for statement in statements)


def test_analysis_service_requires_openai_before_constructing_a_pipeline(monkeypatch):
    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.ensure_startup_maintenance_complete", lambda: None
    )
    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.get_settings",
        lambda: SimpleNamespace(llm_provider="mock"),
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("Provider construction should not occur")

    monkeypatch.setattr("smartapply.pipeline.Pipeline", unexpected)
    with pytest.raises(ValueError, match="OpenAI"):
        AnalysisServiceMixin().start_analysis_batch([42])


def test_analysis_service_dispatches_only_requested_batch_operation(monkeypatch):
    calls = []
    progress = []
    fake_pipeline = object()
    snapshot = workspace(active=True)

    class Coordinator:
        def __init__(self, **kwargs):
            calls.append(("coordinator", kwargs))

        def start(self, ids):
            calls.append(("start", ids))

        def sync(self):
            calls.append(("sync",))

        def retry(self, run_id):
            calls.append(("retry", run_id))

        def cancel(self, run_id):
            calls.append(("cancel", run_id))

    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.ensure_startup_maintenance_complete", lambda: None
    )
    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.get_settings",
        lambda: SimpleNamespace(llm_provider="openai"),
    )
    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.BatchAnalysisCoordinator", Coordinator
    )
    monkeypatch.setattr("smartapply.desktop.analysis_services.analysis_workspace", lambda: snapshot)
    monkeypatch.setattr("smartapply.pipeline.Pipeline", lambda **kwargs: fake_pipeline)
    service = AnalysisServiceMixin()
    assert service.start_analysis_batch([42, 42, -1, 7], progress=progress.append) == snapshot
    assert calls == [("coordinator", {"pipeline": fake_pipeline}), ("start", [42, 7])]
    assert progress == ["Préparation du lot d’analyse…"]
    calls.clear()
    assert service.sync_analysis_batches() == snapshot
    assert calls == [("coordinator", {}), ("sync",)]
    calls.clear()
    assert service.retry_analysis_batch("run-1") == snapshot
    assert calls == [("coordinator", {}), ("retry", "run-1")]
    calls.clear()
    assert service.cancel_analysis_batch("run-1") == snapshot
    assert calls == [("coordinator", {}), ("cancel", "run-1")]


def test_cancelled_batch_preparation_returns_the_existing_workspace(monkeypatch):
    snapshot = workspace()

    class Coordinator:
        def __init__(self, **kwargs):
            pass

        def start(self, _ids):
            raise TaskCancelledError()

    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.ensure_startup_maintenance_complete", lambda: None
    )
    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.get_settings",
        lambda: SimpleNamespace(llm_provider="openai"),
    )
    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.BatchAnalysisCoordinator", Coordinator
    )
    monkeypatch.setattr("smartapply.desktop.analysis_services.analysis_workspace", lambda: snapshot)
    monkeypatch.setattr("smartapply.pipeline.Pipeline", lambda **kwargs: object())
    assert (
        AnalysisServiceMixin().start_analysis_batch([42], stop_requested=lambda: True) == snapshot
    )


def test_immediate_service_analyzes_only_selected_offers(monkeypatch):
    calls = []
    report = SimpleNamespace(analyzed=1, errors=[])
    snapshot = workspace()

    class Pipeline:
        def __init__(self, **kwargs):
            calls.append(("pipeline", kwargs))

        def prepare_analysis(self, *, job_ids):
            calls.append(("prepare", job_ids))

        def analyze_jobs(self, ids):
            calls.append(("analyze", ids))
            return report

    monkeypatch.setattr(
        "smartapply.desktop.analysis_services.ensure_startup_maintenance_complete", lambda: None
    )
    monkeypatch.setattr("smartapply.desktop.analysis_services.analysis_workspace", lambda: snapshot)
    monkeypatch.setattr("smartapply.pipeline.Pipeline", Pipeline)
    result = AnalysisServiceMixin().start_immediate_analysis([7, 7, -1])
    assert result == {"workspace": snapshot, "report": report}
    assert calls == [
        ("pipeline", {"stop_requested": None}),
        ("prepare", [7]),
        ("analyze", [7]),
    ]


def test_cancel_and_monitor_batch_remain_available_during_immediate_analysis(make_bridge):
    bridge = make_bridge()
    controller = bridge._analysis_controller
    cancelled = []
    bridge.service.cancel_analysis_batch = lambda run_id: cancelled.append(run_id) or workspace()
    bridge.service.sync_analysis_batches = lambda: workspace(active=True)
    bridge.startImmediateAnalysis([42])
    immediate = controller._pool.pending[0]
    bridge.refreshAnalysisWorkspace()
    assert controller._monitor_pool.pending
    controller._monitor_pool.finish_next()
    bridge.cancelAnalysisBatch("run-1")
    controller._monitor_pool.finish_next()
    assert cancelled == ["run-1"]
    assert controller._immediate_worker is immediate
    assert controller.loading and controller.operation == "immediate"
    bridge.cancelImmediateAnalysis()
    assert immediate.cancel_requested
    controller._pool.finish_next()
    assert not controller.loading


def test_cancel_requested_during_polling_is_queued_once_instead_of_lost(make_bridge):
    bridge = make_bridge()
    cancelled = []
    bridge.service.sync_analysis_batches = lambda: workspace(active=True)
    bridge.service.cancel_analysis_batch = lambda run_id: cancelled.append(run_id) or workspace()
    bridge.refreshAnalysisWorkspace()
    bridge.cancelAnalysisBatch("run-1")
    bridge.cancelAnalysisBatch("run-1")
    assert cancelled == []
    bridge._analysis_controller._pool.finish_next()
    QCoreApplication.processEvents()
    bridge._analysis_controller._pool.finish_next()
    assert cancelled == ["run-1"]


def test_empty_desktop_batch_selection_never_expands_to_entire_database(isolated_db, monkeypatch):
    from smartapply.desktop.analysis_services import AnalysisServiceMixin

    monkeypatch.setattr("smartapply.desktop.analysis_services.ensure_startup_maintenance_complete", lambda: None)
    monkeypatch.setattr("smartapply.desktop.analysis_services.get_settings", lambda: SimpleNamespace(llm_provider="openai"))
    with pytest.raises(ValueError, match="Sélectionnez"):
        AnalysisServiceMixin().start_analysis_batch([])
