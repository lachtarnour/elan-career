"""Generation controls and progress remain isolated from offer browsing."""

import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.generation_services import GenerationServiceMixin


class ControlledPool:
    def __init__(self):
        self.pending = []

    def start(self, worker):
        self.pending.append(worker)

    def finish_next(self):
        self.pending.pop(0).run()


def workspace(status="in_progress", *, generated=0, started=False):
    result = {
        "runs": [{
            "id": "generation-1", "kind": "generation", "status": status,
            "is_active": status == "in_progress", "generated": generated,
            "failed": 0, "can_cancel": status == "in_progress",
        }],
        "active_run_id": "generation-1" if status == "in_progress" else "",
    }
    if started:
        result["started_run_id"] = "generation-1"
    return result


@pytest.fixture
def make_bridge(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    for name in ("refreshDashboard", "refreshShortlist", "refreshProfile", "refreshDiagnosticsIfStale"):
        monkeypatch.setattr(DesktopBridge, name, lambda self: None)
    bridges = []

    def make(*, startup=False):
        service = SimpleNamespace(
            initialize=lambda: None, analysis_top_k=lambda: 25,
            shortlist_top_k=lambda: 25, startup_maintenance_needed=lambda: False,
            generation_workspace=lambda: workspace(),
            start_generation_batch=lambda ids, **kwargs: workspace(started=True),
            start_immediate_generation=lambda ids, **kwargs: workspace("completed", generated=1, started=True),
            retry_generation_run=lambda run_id, **kwargs: workspace(started=True),
            cancel_generation_run=lambda run_id: workspace("paused"),
            abandon_generation_run=lambda run_id: workspace("cancelled"),
        )
        if startup:
            service.sync_generation_runs = lambda: workspace()
        bridge = DesktopBridge(service)
        bridge._thread_pool = ControlledPool()
        bridge._read_pool = ControlledPool()
        bridge._mutation_pool = ControlledPool()
        bridge._generation_controller._pool = ControlledPool()
        bridge._generation_controller._monitor_pool = ControlledPool()
        bridges.append(bridge)
        return bridge

    yield make
    for bridge in bridges:
        bridge._shutdown_requested = True
        bridge._generation_controller.stop()
        bridge._analysis_controller.stop()
        bridge._workflow_refresh_timer.stop()
        bridge._busy_jobs_controller.stop()
        assert bridge._busy_jobs_controller._pool.waitForDone(3_000)
        for pool in (bridge._generation_controller._pool, bridge._generation_controller._monitor_pool):
            while pool.pending:
                pool.finish_next()
        bridge._unsubscribe_resources()
        bridge._unsubscribe_database()
    app.processEvents()


def test_immediate_progress_uses_read_only_monitor_and_preserves_offers(make_bridge):
    bridge = make_bridge()
    bridge._selected_job_id = 42
    bridge._current_job = {"id": 42, "application": {"notes": "draft"}}
    bridge._job_search = "Python"
    original = deepcopy(bridge.currentJob)
    calls = []
    bridge.service.sync_generation_runs = lambda: calls.append("sync") or workspace()
    bridge.service.generation_workspace = lambda: calls.append("read") or workspace()
    bridge.startImmediateGeneration([42, 42, -1])
    controller = bridge._generation_controller
    assert bridge.generationWorkspaceOperation == "immediate" and bridge.busy
    assert [state["lane"] for state in bridge.longTaskStates] == ["generation"]
    assert not bridge.searchRunning
    assert controller._pool.pending[0].args == ([42],)
    controller.refresh()
    controller._monitor_pool.finish_next()
    assert calls == ["read"] and bridge.generationWorkspaceOperation == "immediate"
    assert bridge.generationWorkspace["active_run_id"] == "generation-1"
    assert controller._timer.interval() == 1_000
    assert bridge.currentJob == original and bridge._job_search == "Python"
    assert not bridge._thread_pool.pending
    bridge.cancelImmediateGeneration()
    assert controller._pool.pending[0].cancel_requested


def test_startup_monitors_generation_without_opening_offers(make_bridge):
    bridge = make_bridge(startup=True)
    QCoreApplication.processEvents()
    controller = bridge._generation_controller
    assert len(controller._monitor_pool.pending) == 1
    controller._monitor_pool.finish_next()
    assert bridge.generationWorkspace["active_run_id"] == "generation-1"
    assert controller._timer.isActive() and controller._timer.interval() == 120_000


def test_cancel_is_available_during_immediate_and_waits_for_monitor(make_bridge):
    bridge = make_bridge()
    controller = bridge._generation_controller
    cancelled = []
    bridge.service.cancel_generation_run = lambda run_id: cancelled.append(run_id) or workspace("paused")
    bridge.startImmediateGeneration([42])
    controller.refresh()
    bridge.cancelGenerationRun("generation-1")
    controller._monitor_pool.finish_next()
    QCoreApplication.processEvents()
    controller._monitor_pool.finish_next()
    assert cancelled == ["generation-1"]
    assert bridge.generationWorkspaceOperation == "immediate"
    assert bridge.generationWorkspace["runs"][0]["status"] == "paused"


def test_cancelled_batch_preparation_cancels_created_run(make_bridge):
    bridge = make_bridge()
    controller = bridge._generation_controller
    cancelled = []
    bridge.service.cancel_generation_run = lambda run_id: cancelled.append(run_id) or workspace("paused")
    bridge.startGenerationBatch([42])
    bridge.cancelImmediateGeneration()
    controller._pool.finish_next()
    QCoreApplication.processEvents()
    controller._monitor_pool.finish_next()
    assert cancelled == ["generation-1"]


def test_transient_generation_error_keeps_progress_and_polling(make_bridge):
    bridge = make_bridge()
    controller = bridge._generation_controller
    controller._loaded(workspace())
    original = deepcopy(bridge.generationWorkspace)

    def fail():
        raise RuntimeError("Connexion temporairement indisponible")

    bridge.service.sync_generation_runs = fail
    controller.refresh()
    controller._monitor_pool.finish_next()
    assert bridge.generationWorkspace == original
    assert bridge.generationWorkspaceError == "Connexion temporairement indisponible"
    assert controller._timer.isActive()


def test_late_progress_read_cannot_replace_completed_command(make_bridge):
    bridge = make_bridge()
    controller = bridge._generation_controller
    bridge.startImmediateGeneration([42])
    controller.refresh()
    controller._pool.finish_next()
    assert bridge.generationWorkspace["runs"][0]["status"] == "completed"
    controller._monitor_pool.finish_next()
    assert bridge.generationWorkspace["runs"][0]["status"] == "completed"


def test_generation_completion_is_notified_once_and_retry_can_finish(make_bridge):
    bridge = make_bridge()
    notifications = []
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    controller = bridge._generation_controller
    controller._loaded(workspace())
    controller._loaded(workspace("paused", generated=1))
    controller._loaded(workspace("paused", generated=1))
    assert len(notifications) == 1 and notifications[0][0] == "Création arrêtée"
    bridge.service.retry_generation_run = lambda run_id, **kwargs: workspace("completed", generated=2)
    bridge.retryGenerationRun("generation-1")
    controller._pool.finish_next()
    assert len(notifications) == 2 and notifications[1][0] == "Création terminée"


def test_shutdown_cancels_generation_and_waits_for_cleanup(make_bridge):
    bridge = make_bridge()
    controller = bridge._generation_controller
    bridge.startImmediateGeneration([42])
    bridge.requestShutdown()
    assert controller._pool.pending[0].cancel_requested
    assert not bridge.shutdownReady and not controller._timer.isActive()
    controller._pool.finish_next()
    assert bridge.shutdownReady and bridge.generationWorkspace == {"runs": [], "active_run_id": ""}


def test_shortlist_generation_snapshots_ids_before_dispatch(make_bridge):
    bridge = make_bridge()
    bridge.service.shortlisted_generation_ids = lambda: [42, 42, 7]
    bridge.startShortlistedGeneration("batch")
    bridge._read_pool.finish_next()
    assert bridge._generation_controller._pool.pending[0].args == ([42, 7],)


def test_abandon_waits_for_monitor_and_refreshes_released_offers(make_bridge):
    bridge = make_bridge()
    controller = bridge._generation_controller
    calls = []
    bridge.service.abandon_generation_run = lambda run_id: calls.append(run_id) or workspace("cancelled")
    controller.refresh(sync=False)
    bridge.abandonGenerationRun("generation-1")
    controller._monitor_pool.finish_next()
    QCoreApplication.processEvents()
    controller._monitor_pool.finish_next()
    assert calls == ["generation-1"]
    assert bridge.generationWorkspace["runs"][0]["status"] == "cancelled"


def test_generation_facade_dispatches_mode_and_resume_without_extra_calls(monkeypatch):
    calls = []

    def stop():
        return False

    class Coordinator:
        def start(self, ids, **kwargs):
            calls.append(("start", ids, kwargs))
            return "generation-1"

        def sync(self):
            calls.append(("sync",))

        def retry(self, run_id, **kwargs):
            calls.append(("retry", run_id, kwargs))

        def cancel(self, run_id):
            calls.append(("cancel", run_id))

        def abandon(self, run_id):
            calls.append(("abandon", run_id))

    monkeypatch.setitem(sys.modules, "smartapply.pipeline.generation.coordinator", SimpleNamespace(
        GenerationCoordinator=Coordinator,
    ))
    monkeypatch.setitem(sys.modules, "smartapply.pipeline.generation.presentation", SimpleNamespace(
        generation_workspace=lambda: workspace(),
    ))
    monkeypatch.setattr("smartapply.desktop.generation_services.ensure_startup_maintenance_complete", lambda: None)
    service = GenerationServiceMixin()
    assert service.start_generation_batch([42, 42, -1, 7], stop_requested=stop)["started_run_id"] == "generation-1"
    service.start_immediate_generation([7], stop_requested=stop)
    service.retry_generation_run("generation-1", stop_requested=stop)
    service.cancel_generation_run("generation-1")
    service.abandon_generation_run("generation-1")
    service.sync_generation_runs()
    assert calls == [
        ("start", [42, 7], {"mode": "batch", "stop_requested": stop, "progress": None}),
        ("start", [7], {"mode": "immediate", "stop_requested": stop, "progress": None}),
        ("retry", "generation-1", {"stop_requested": stop}),
        ("cancel", "generation-1"), ("abandon", "generation-1"), ("sync",),
    ]
    with pytest.raises(ValueError, match="Sélectionnez"):
        service.start_generation_batch([])
    assert len(calls) == 6


def test_generation_workspace_displays_each_offers_original_routes(isolated_db):
    from smartapply.database import write_session_scope
    from smartapply.database.generation_runs import (
        GenerationItem,
        GenerationItemContext,
        GenerationRun,
    )
    from smartapply.database.models import Job
    from smartapply.pipeline.generation.presentation import generation_workspace

    with write_session_scope() as session:
        session.add(GenerationRun(
            id="mixed-resume", mode="immediate", state="completed", profile_data={},
            routes={"cv_adaptation": {"provider": "openai", "model": "current-model"}},
            reference_date="2026-10-11",
        ))
        session.flush()
        items = []
        for number in (1, 2):
            job = Job(external_id=f"route-{number}", title="AI Engineer", source_company="Acme",
                      description="Python ML", source="manual")
            session.add(job)
            session.flush()
            item = GenerationItem(
                run_id="mixed-resume", job_id=job.id, state="completed", job_snapshot="saved",
                analysis_snapshot="saved", title=job.title, company=job.source_company,
            )
            session.add(item)
            session.flush()
            items.append(item)
        session.add(GenerationItemContext(
            item_id=items[0].id, profile_data={"private": "original profile"},
            routes={"cv_adaptation": {"provider": "anthropic", "model": "original-model"}},
            reference_date="2026-10-10",
        ))
    result = generation_workspace()
    assert result["runs"][0]["provider_label"] == "anthropic · openai"
    assert result["runs"][0]["model"] == "original-model · current-model"
    assert "original profile" not in str(result)
