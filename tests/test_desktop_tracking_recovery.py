"""Reopening tracking publishes its persisted journal before provider recovery."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.generation_runs import GenerationItem, GenerationRun
from smartapply.desktop.analysis_controller import AnalysisController
from smartapply.desktop.generation_controller import GenerationController
from smartapply.desktop.workspace_views import analysis_workspace_view, generation_workspace_view
from tests.test_desktop_workflow_concurrency import ControlledPool, wait_until
from tests.test_desktop_workflow_concurrency import make_bridge as make_bridge
from tests.test_desktop_workspace_navigation import view_result
from tests.test_desktop_workspace_views import _history


class TrackingBridge(QObject):
    busyChanged = Signal()
    toastRequested = Signal(str, str, str)

    def __init__(self, service):
        super().__init__()
        self.service = service
        self.shutdownRequested = False
        self._workers = set()

    def _check_shutdown(self):
        pass

    def _refresh_workflow(self):
        pass


@pytest.fixture
def tracking_controller(isolated_db):
    application = QCoreApplication.instance() or QCoreApplication([])
    controllers = []

    def make(kind):
        _history(kind, 26, active_oldest=True)
        calls = []
        read = analysis_workspace_view if kind == "analysis" else generation_workspace_view

        def local_read(**options):
            calls.append("read")
            return read(**options)

        def unavailable_sync(**options):
            calls.append("sync")
            if getattr(service, "before_sync_failure", None) is not None:
                service.before_sync_failure()
            raise ConnectionError("Simulated provider unavailable")

        sync_name = "sync_analysis_batches" if kind == "analysis" else "sync_generation_runs"
        service = SimpleNamespace(**{
            f"{kind}_workspace": local_read,
            f"{kind}_workspace_view": local_read,
            sync_name: unavailable_sync,
            f"{sync_name}_view": unavailable_sync,
        })
        bridge = TrackingBridge(service)
        controller_cls = AnalysisController if kind == "analysis" else GenerationController
        controller = controller_cls(bridge)
        controller._pool = ControlledPool()
        controller._monitor_pool = ControlledPool()
        pool = controller._pool if kind == "analysis" else controller._monitor_pool
        controllers.append((bridge, controller))
        return controller, pool, calls, local_read, unavailable_sync

    yield make
    for bridge, controller in controllers:
        bridge.shutdownRequested = True
        controller.stop()
        for pool in (controller._pool, controller._monitor_pool):
            while pool.pending:
                pool.finish_next()
    application.processEvents()


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_startup_reads_persisted_journal_before_provider_reconciliation(tracking_controller, kind):
    controller, pool, calls, local_read, unavailable_sync = tracking_controller(kind)
    QCoreApplication.processEvents()
    assert len(pool.pending) == 1
    assert pool.pending[0].fn is local_read
    assert calls == []
    pool.finish_next()
    assert calls == ["read"]
    assert controller.workspace["history_total_count"] == 26
    assert controller.workspace["selected_run"]["id"] == "run-0000"
    assert controller.workspace["selected_run"]["items"]
    assert controller.workspace["reserved_job_ids"]
    # Reconciliation is scheduled automatically, but it cannot delay the
    # visible persisted journal or become a prerequisite for opening it.
    QCoreApplication.processEvents()
    assert len(pool.pending) == 1
    assert pool.pending[0].fn is unavailable_sync
    assert calls == ["read"]


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_failed_reconciliation_keeps_persisted_tracking_and_history_navigation(tracking_controller, kind):
    controller, pool, calls, local_read, unavailable_sync = tracking_controller(kind)
    QCoreApplication.processEvents()
    assert pool.pending[0].fn is local_read
    pool.finish_next()
    persisted = deepcopy(controller.workspace)
    QCoreApplication.processEvents()
    assert pool.pending[0].fn is unavailable_sync
    pool.finish_next()
    assert calls == ["read", "sync"]
    assert controller.workspace == persisted
    assert "Simulated provider unavailable" in controller.error
    assert not controller.loading
    assert controller._timer.isActive()
    controller.set_view(history_page=2, selected_run_id="run-0001")
    assert len(pool.pending) == 1 and pool.pending[0].fn is local_read
    pool.finish_next()
    assert calls == ["read", "sync", "read"]
    assert controller.workspace["history_page"] == 2
    assert controller.workspace["selected_run"]["id"] == "run-0001"
    assert controller.workspace["selected_run"]["items"]
    assert controller.workspace["reserved_job_ids"] == persisted["reserved_job_ids"]
    assert controller._timer.isActive()


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_sync_failure_after_successful_journal_read_never_erases_existing_tracking(tracking_controller, kind):
    controller, pool, calls, local_read, unavailable_sync = tracking_controller(kind)
    # Isolate the existing refresh failure contract from automatic startup.
    # Publication of a local journal already exists in the current controller.
    controller.refresh(sync=False)
    assert pool.pending[0].fn is local_read
    pool.finish_next()
    persisted = deepcopy(controller.workspace)
    controller.refresh(sync=True)
    assert pool.pending[0].fn is unavailable_sync
    pool.finish_next()
    assert calls == ["read", "sync"]
    assert controller.workspace == persisted
    assert controller.workspace["selected_run"]["items"]
    assert controller._timer.isActive()


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_failed_sync_rereads_partial_commits_and_preserves_error_without_resubmission(tracking_controller, kind):
    controller, pool, calls, local_read, unavailable_sync = tracking_controller(kind)
    QCoreApplication.processEvents()
    pool.finish_next()
    QCoreApplication.processEvents()
    assert pool.pending[0].fn is unavailable_sync
    run_cls = AnalysisBatchRun if kind == "analysis" else GenerationRun
    item_cls = AnalysisBatchItem if kind == "analysis" else GenerationItem

    def partial_progress():
        with session_scope() as session:
            run = session.get(run_cls, "run-0000")
            run.state = "completed"
            for item in session.query(item_cls).filter_by(run_id=run.id):
                item.state = "completed"

    controller.bridge.service.before_sync_failure = partial_progress
    pool.finish_next()
    assert calls == ["read", "sync"]
    QCoreApplication.processEvents()
    assert len(pool.pending) == 1 and pool.pending[0].fn is local_read
    pool.finish_next()
    assert calls == ["read", "sync", "read"]
    assert controller.workspace["selected_run"]["status"] == "completed"
    assert controller.workspace["selected_run"]["items"][0]["state"] == "completed"
    assert controller.workspace["reserved_job_ids"] == []
    assert "Simulated provider unavailable" in controller.error
    assert not controller._timer.isActive()
    for _ in range(3):
        controller.refresh(sync=False)
        assert pool.pending[0].fn is local_read
        pool.finish_next()
    assert calls.count("sync") == 1
    assert "Simulated provider unavailable" in controller.error


def test_opening_tracking_reads_both_journals_without_changing_offer_context(make_bridge, monkeypatch):
    bridge = make_bridge()
    calls = []
    rows = [{"id": 42, "title": "Selected offer", "status": "analyzed"}]
    bridge._jobs = rows
    bridge._jobs_model.replace(rows)
    bridge._selected_job_id = 42
    bridge._current_job = {"id": 42, "application": {"id": 7, "notes": "Unsaved notes"}}
    bridge._jobs_page = 3
    bridge._job_status = "analyzed"
    bridge._job_sort_key = "llm_score"
    bridge._job_sort_ascending = False
    bridge._notes_failures[7] = ("Unsaved notes", 1)
    selected = bridge._current_job

    def analysis_read(**options):
        calls.append("analysis_read")
        return view_result("analysis", options)

    def generation_read(**options):
        calls.append("generation_read")
        return view_result("generation", options, candidates=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Opening tracking must only read its local journals")

    bridge.service.analysis_workspace_view = analysis_read
    bridge.service.generation_workspace_view = generation_read
    bridge.service.sync_analysis_batches = forbidden
    bridge.service.sync_analysis_batches_view = forbidden
    bridge.service.sync_generation_runs = forbidden
    bridge.service.sync_generation_runs_view = forbidden
    bridge.service.list_jobs = forbidden
    bridge.service.list_jobs_page = forbidden
    bridge.service.get_job = forbidden
    monkeypatch.setattr(bridge, "loadJobs", forbidden)
    monkeypatch.setattr(bridge, "refreshSelectedJob", forbidden)
    bridge.setActiveRoute("tracking")
    assert bridge._analysis_controller._pool.pending[0].fn is analysis_read
    assert bridge._generation_controller._monitor_pool.pending[0].fn is generation_read
    bridge._analysis_controller._pool.finish_next()
    bridge._generation_controller._monitor_pool.finish_next()
    assert calls == ["analysis_read", "generation_read"]
    assert bridge.jobs is rows and bridge._jobs_model._rows == rows
    assert bridge._selected_job_id == 42 and bridge._current_job is selected
    assert bridge._jobs_page == 3 and bridge._job_status == "analyzed"
    assert bridge._job_sort_key == "llm_score" and bridge._job_sort_ascending is False
    assert bridge._notes_failures == {7: ("Unsaved notes", 1)}


@pytest.fixture
def batch_submission(make_bridge, isolated_db):
    def make(kind, *, retry=False, acknowledged_before_stop=False):
        _history(kind, 26, active_oldest=True)
        bridge = make_bridge()
        controller = getattr(bridge, f"_{kind}_controller")
        run_cls = AnalysisBatchRun if kind == "analysis" else GenerationRun
        reader = analysis_workspace_view if kind == "analysis" else generation_workspace_view
        observations, cancellations = [], []
        controller._loaded(reader(selected_run_id="run-0000"))

        def finish_submission(ids_or_run, *, stop_requested, view):
            stopped = stop_requested()
            observations.append(stopped)
            with session_scope() as session:
                run = session.get(run_cls, "run-0000")
                # A provider can acknowledge just before Stop is observed.
                # In that case the controller must finalize the explicit
                # cancellation through the durable run, even during close.
                cancelled = stopped and not acknowledged_before_stop
                run.cancel_requested = cancelled
                run.state = "cancelled" if cancelled else "active"
            result = reader(**view)
            result["started_run_id"] = "run-0000"
            return result

        def cancel(run_id, **kwargs):
            cancellations.append(run_id)
            with session_scope() as session:
                run = session.get(run_cls, run_id)
                run.cancel_requested = True
                run.state = "cancelled"
            return reader(**kwargs.get("view", {}))

        setattr(bridge.service, f"{kind}_workspace_view", reader)
        if kind == "analysis":
            bridge.service.start_analysis_batch = finish_submission
            bridge.service.cancel_analysis_batch = cancel
            bridge.startAnalysisBatch([1])
        else:
            bridge.service.start_generation_batch = finish_submission
            bridge.service.retry_generation_run = finish_submission
            bridge.service.cancel_generation_run = cancel
            if retry:
                bridge.retryGenerationRun("run-0000")
            else:
                bridge.startGenerationBatch([1])
        pool = controller._pool
        assert len(pool.pending) == 1 and pool.pending[0].with_cancel
        return bridge, controller, pool, run_cls, observations, cancellations

    return make


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_closing_during_batch_submission_waits_for_acknowledgement_without_remote_cancel(batch_submission, kind):
    bridge, controller, pool, run_cls, observations, cancellations = batch_submission(kind)
    worker = pool.pending[0]
    bridge.requestShutdown()
    assert bridge.shutdownRequested and not bridge.shutdownReady
    assert worker.cancel_requested is False
    assert not controller._timer.isActive()
    pool.finish_next()
    wait_until(lambda: bridge.shutdownReady)
    assert observations == [False] and cancellations == []
    with session_scope() as session:
        persisted = session.get(run_cls, "run-0000")
        assert persisted.state == "active" and persisted.cancel_requested is False


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_explicit_stop_still_cancels_batch_submission(batch_submission, kind):
    bridge, _controller, pool, run_cls, observations, _cancellations = batch_submission(kind)
    worker = pool.pending[0]
    bridge.cancelCurrentTask()
    assert not bridge.shutdownRequested
    assert worker.cancel_requested is True
    pool.finish_next()
    assert observations == [True]
    with session_scope() as session:
        persisted = session.get(run_cls, "run-0000")
        assert persisted.cancel_requested is True and persisted.state == "cancelled"


def test_closing_batch_retry_preserves_submission_after_navigation_to_another_mode(batch_submission):
    bridge, controller, pool, run_cls, observations, cancellations = batch_submission("generation", retry=True)
    worker = pool.pending[0]
    controller.workspace = {
        "desktop_view": True, "runs": [], "monitor_runs": [],
        "selected_run": {"id": "unrelated-immediate", "mode": "immediate", "is_active": False},
    }
    bridge.requestShutdown()
    assert not worker.cancel_requested and not bridge.shutdownReady
    pool.finish_next()
    wait_until(lambda: bridge.shutdownReady)
    assert observations == [False] and cancellations == []
    with session_scope() as session:
        persisted = session.get(run_cls, "run-0000")
        assert persisted.state == "active" and persisted.cancel_requested is False


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_shutdown_drains_explicit_cancellation_queued_behind_monitor(make_bridge, isolated_db, kind):
    _history(kind, 26, active_oldest=True)
    bridge = make_bridge()
    controller = getattr(bridge, f"_{kind}_controller")
    reader = analysis_workspace_view if kind == "analysis" else generation_workspace_view
    run_cls = AnalysisBatchRun if kind == "analysis" else GenerationRun
    calls = []

    def cancel(run_id, *, view):
        calls.append(run_id)
        with session_scope() as session:
            run = session.get(run_cls, run_id)
            run.cancel_requested = True
            run.state = "cancelled"
        return reader(**view)

    setattr(bridge.service, f"{kind}_workspace_view", reader)
    setattr(bridge.service, "cancel_analysis_batch" if kind == "analysis" else "cancel_generation_run", cancel)
    controller._loaded(reader(selected_run_id="run-0000"))
    controller.refresh(sync=False)
    pool = controller._pool if kind == "analysis" else controller._monitor_pool
    assert len(pool.pending) == 1 and pool.pending[0].fn is reader
    if kind == "analysis":
        bridge.cancelAnalysisBatch("run-0000")
    else:
        bridge.cancelGenerationRun("run-0000")
    assert controller._pending_cancellations == ["run-0000"]
    bridge.requestShutdown()
    assert not bridge.shutdownReady and calls == []
    pool.finish_next()
    # Dispatch synchronously: readiness cannot become true between these
    # workers, even before another Qt event loop iteration.
    assert len(pool.pending) == 1 and pool.pending[0].fn is cancel
    assert not bridge.shutdownReady and calls == []
    assert pool.pending[0] in bridge._workers
    pool.finish_next()
    wait_until(lambda: bridge.shutdownReady)
    assert calls == ["run-0000"]
    with session_scope() as session:
        persisted = session.get(run_cls, "run-0000")
        assert persisted.cancel_requested is True and persisted.state == "cancelled"


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_stop_then_close_cancels_batch_acknowledged_after_stop(batch_submission, kind):
    bridge, controller, submission_pool, run_cls, observations, cancellations = batch_submission(
        kind, acknowledged_before_stop=True,
    )
    worker = submission_pool.pending[0]
    bridge.cancelCurrentTask()
    assert worker.cancel_requested
    bridge.requestShutdown()
    assert worker.cancel_requested and not bridge.shutdownReady
    submission_pool.finish_next()
    assert observations == [True] and cancellations == []
    cancel_pool = controller._pool if kind == "analysis" else controller._monitor_pool
    assert len(cancel_pool.pending) == 1 and cancel_pool.pending[0] in bridge._workers
    assert not bridge.shutdownReady
    with session_scope() as session:
        persisted = session.get(run_cls, "run-0000")
        assert persisted.state == "active" and persisted.cancel_requested is False
    cancel_pool.finish_next()
    wait_until(lambda: bridge.shutdownReady)
    assert cancellations == ["run-0000"]
    with session_scope() as session:
        persisted = session.get(run_cls, "run-0000")
        assert persisted.cancel_requested is True and persisted.state == "cancelled"


def test_shutdown_drains_explicit_abandon_queued_behind_generation_monitor(make_bridge, isolated_db):
    _history("generation", 26, active_oldest=True)
    bridge = make_bridge()
    controller = bridge._generation_controller
    calls = []

    def abandon(run_id, *, view):
        calls.append(run_id)
        with session_scope() as session:
            session.get(GenerationRun, run_id).state = "cancelled"
        return generation_workspace_view(**view)

    bridge.service.generation_workspace_view = generation_workspace_view
    bridge.service.abandon_generation_run = abandon
    controller._loaded(generation_workspace_view(selected_run_id="run-0000"))
    controller.refresh(sync=False)
    pool = controller._monitor_pool
    assert len(pool.pending) == 1
    bridge.abandonGenerationRun("run-0000")
    assert controller._pending_abandons == ["run-0000"]
    bridge.requestShutdown()
    assert not bridge.shutdownReady
    pool.finish_next()
    assert len(pool.pending) == 1 and pool.pending[0].fn is abandon
    assert not bridge.shutdownReady and calls == []
    pool.finish_next()
    wait_until(lambda: bridge.shutdownReady)
    assert calls == ["run-0000"]
    with session_scope() as session:
        assert session.get(GenerationRun, "run-0000").state == "cancelled"
