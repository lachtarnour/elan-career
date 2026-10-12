"""Independent desktop workflows run together without sharing cancellation or progress."""

from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from smartapply.desktop.bridge import DesktopBridge


class ControlledPool:
    def __init__(self):
        self.pending = []

    def start(self, worker):
        self.pending.append(worker)

    def finish(self, worker):
        self.pending.remove(worker)
        worker.run()

    def finish_next(self):
        self.finish(self.pending[0])


def wait_until(predicate, *, timeout=3):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        sleep(0.005)
    QCoreApplication.processEvents()
    assert predicate(), "The Qt worker did not reach the expected state before the deadline"


def task_states(bridge):
    values = bridge.longTaskStates
    if isinstance(values, dict):
        return {lane: {"lane": lane, **state} for lane, state in values.items()}
    return {state["lane"]: state for state in values}


@pytest.fixture
def make_bridge(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    for name in (
        "refreshDashboard", "refreshShortlist", "refreshProfile", "refreshDiagnosticsIfStale",
        "_refresh_workflow",
    ):
        monkeypatch.setattr(DesktopBridge, name, lambda self: None)
    for name in ("_search_done", "_generation_done"):
        monkeypatch.setattr(DesktopBridge, name, lambda self, report: None)
    monkeypatch.setattr(DesktopBridge, "_analysis_done", lambda self, job, report: None)
    bridges, releases = [], []

    def make(*, real_workers=False):
        service = SimpleNamespace(
            initialize=lambda: None,
            analysis_top_k=lambda: 25,
            shortlist_top_k=lambda: 25,
            startup_maintenance_needed=lambda: False,
        )
        bridge = DesktopBridge(service)
        if not real_workers:
            bridge._thread_pool = ControlledPool()
            bridge._analysis_controller._pool = ControlledPool()
            bridge._analysis_controller._monitor_pool = ControlledPool()
            bridge._generation_controller._pool = ControlledPool()
            bridge._generation_controller._monitor_pool = ControlledPool()
        bridge._read_pool = ControlledPool()
        bridge._mutation_pool = ControlledPool()
        bridges.append(bridge)
        return bridge

    make.releases = releases
    yield make
    for event in releases:
        event.set()
    for bridge in bridges:
        bridge.requestShutdown()
        pools = (
            bridge._thread_pool, bridge._read_pool, bridge._mutation_pool,
            bridge._analysis_controller._pool, bridge._analysis_controller._monitor_pool,
            bridge._generation_controller._pool, bridge._generation_controller._monitor_pool,
            bridge._busy_jobs_controller._pool,
        )
        for pool in pools:
            if isinstance(pool, ControlledPool):
                while pool.pending:
                    pool.finish_next()
            else:
                assert pool.waitForDone(3_000)
        app.processEvents()
        bridge._unsubscribe_resources()
        bridge._unsubscribe_database()


def blocked_call(make_bridge, entered, release, observations, lane, result=None):
    make_bridge.releases.append(release)

    def call(*args, progress=None, stop_requested=None, **kwargs):
        if progress is not None:
            progress(f"{lane} en cours")
        entered.set()
        assert release.wait(10), f"The {lane} test worker was not released"
        observations[lane] = bool(stop_requested and stop_requested())
        return result

    call.__name__ = lane
    return call


def test_public_search_analysis_and_generation_slots_enter_before_any_finishes(make_bridge):
    bridge = make_bridge(real_workers=True)
    entered = {lane: Event() for lane in ("search", "analysis", "generation")}
    release = Event()
    observations = {}
    bridge.service.search_jobs = blocked_call(
        make_bridge, entered["search"], release, observations, "search",
    )
    bridge.service.analyze_job = blocked_call(
        make_bridge, entered["analysis"], release, observations, "analysis",
    )
    bridge.service.generate_application = blocked_call(
        make_bridge, entered["generation"], release, observations, "generation",
    )

    bridge.searchJobs("AI Engineer", "France", ["wttj"], 20, "week")
    bridge.analyzeJob(41)
    bridge.generateApplication(42)

    # Every service is actually executing while all completion gates remain closed.
    wait_until(lambda: all(event.is_set() for event in entered.values()))
    assert not release.is_set()
    assert bridge._thread_pool.maxThreadCount() >= 3
    assert bridge.queuedTaskCount == 0
    assert set(task_states(bridge)) == {"search", "analysis", "generation"}
    assert bridge.searchRunning
    release.set()
    wait_until(lambda: not bridge.busy)
    assert observations == {"search": False, "analysis": False, "generation": False}


def test_lane_cancellation_keeps_other_running_and_queued_work(make_bridge):
    bridge = make_bridge()
    observed = []

    def call(lane, *, progress, stop_requested):
        observed.append((lane, stop_requested()))

    for lane in ("search", "analysis", "generation"):
        bridge._run(lane, call, lane, workflow=lane, cancellable=True)
    bridge._run("search queued", call, "search queued", workflow="search", cancellable=True)
    bridge._run("analysis queued", call, "analysis queued", workflow="analysis", cancellable=True)
    active = {lane: state["worker"] for lane, state in bridge._active_tasks.items()}

    bridge.cancelTaskLane("search")

    assert active["search"].cancel_requested
    assert not active["analysis"].cancel_requested
    assert not active["generation"].cancel_requested
    assert bridge.queuedTaskCount == 1
    bridge._thread_pool.finish(active["search"])
    assert not bridge.searchRunning
    bridge._thread_pool.finish(active["generation"])
    bridge._thread_pool.finish(active["analysis"])
    bridge._thread_pool.finish_next()
    assert observed == [
        ("search", True), ("generation", False), ("analysis", False), ("analysis queued", False),
    ]
    assert not bridge.busy


def test_cancel_search_does_not_cancel_analysis_or_generation(make_bridge):
    bridge = make_bridge()
    for lane in ("search", "analysis", "generation"):
        bridge._run(lane, lambda **kwargs: None, workflow=lane, task_key=lane, cancellable=True)
    workers = {lane: state["worker"] for lane, state in bridge._active_tasks.items()}

    bridge.cancelSearch()

    assert workers["search"].cancel_requested
    assert not workers["analysis"].cancel_requested
    assert not workers["generation"].cancel_requested


@pytest.mark.parametrize("lane", ["analysis", "generation"])
def test_cancel_lane_invalidates_slow_launch_intent_without_stopping_search(make_bridge, lane):
    bridge = make_bridge()
    bridge.service.set_analysis_top_k = lambda value: value
    bridge.service.pending_analysis_ids = lambda: [41]
    bridge.service.process_pending = lambda **kwargs: {"analyzed": 1, "analysis_errors": []}
    bridge.service.shortlisted_generation_ids = lambda: [42]
    bridge.service.start_generation_batch = lambda ids, **kwargs: {
        "runs": [], "active_run_id": "",
    }
    for workflow in (lane, "search"):
        bridge._run(
            workflow, lambda **kwargs: None,
            workflow=workflow, task_key=workflow, cancellable=True,
        )
    lane_worker = bridge._active_tasks[lane]["worker"]
    search_worker = bridge._active_tasks["search"]["worker"]
    if lane == "analysis":
        bridge.processPending(3)
    else:
        bridge.startShortlistedGeneration("batch")
    assert len(bridge._read_pool.pending) == 1

    bridge.cancelTaskLane(lane)
    bridge._thread_pool.finish(lane_worker)
    # A slow selection snapshot finishes after cancellation and must not launch a paid workflow.
    bridge._read_pool.finish_next()

    assert set(task_states(bridge)) == {"search"}
    assert bridge._thread_pool.pending == [search_worker]
    assert not bridge._generation_controller._pool.pending
    assert not search_worker.cancel_requested and bridge.searchRunning


def test_global_cancel_invalidates_analysis_intent_before_its_lane_exists(make_bridge):
    bridge = make_bridge()
    bridge.service.set_analysis_top_k = lambda value: value
    bridge.service.pending_analysis_ids = lambda: [41]
    bridge.service.process_pending = lambda **kwargs: {"analyzed": 1, "analysis_errors": []}
    bridge._run(
        "search", lambda **kwargs: None,
        workflow="search", task_key="search", cancellable=True,
    )
    search_worker = bridge._active_tasks["search"]["worker"]
    bridge.processPending(3)
    assert set(task_states(bridge)) == {"search"}

    bridge.cancelCurrentTask()
    bridge._read_pool.finish_next()

    assert search_worker.cancel_requested
    assert bridge._thread_pool.pending == [search_worker]
    assert set(task_states(bridge)) == {"search"} and bridge.queuedTaskCount == 0
    bridge._thread_pool.finish(search_worker)
    assert not bridge.busy


def test_shortlist_update_warns_about_busy_offer_and_uses_persisted_summary(make_bridge):
    bridge = make_bridge()
    notifications, summaries = [], []
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    bridge.shortlistChanged.connect(lambda: summaries.append(dict(bridge.shortlist)))

    bridge._shortlist_updated({
        "shortlisted": 2,
        "skipped_busy": 1,
        "skipped_busy_ids": [42],
        "shortlist_summary": {"total": 3, "ready_to_generate": 1},
    })

    assert bridge.shortlist == {"total": 3, "ready_to_generate": 1}
    assert summaries == [{"total": 3, "ready_to_generate": 1}]
    assert len(notifications) == 1 and notifications[0][2] == "warning"
    message = notifications[0][1].casefold()
    assert "1" in message and "occup" in message and "intact" in message
    assert bridge.activity["kind"] == "warning"


@pytest.mark.parametrize("start_slot", ["startAnalysisBatch", "startImmediateAnalysis"])
def test_analysis_start_notifies_visible_active_lane_after_worker_registration(make_bridge, start_slot):
    bridge = make_bridge()
    workspace = {"candidates": [], "runs": [], "active_run_id": ""}
    bridge.service.start_analysis_batch = lambda ids, **kwargs: workspace
    bridge.service.start_immediate_analysis = lambda ids, **kwargs: {
        "workspace": workspace,
        "report": SimpleNamespace(analyzed=1, errors=[], screened_out=0, cancelled=False),
    }
    snapshots = []
    bridge.busyChanged.connect(lambda: snapshots.append((bridge.busy, task_states(bridge))))

    getattr(bridge, start_slot)([41])

    # QML bindings evaluate state at notification time, rather than after this slot returns.
    assert any(
        busy and states.get("analysis", {}).get("cancellable")
        for busy, states in snapshots
    )


@pytest.mark.parametrize("needs_analysis", [True, False])
def test_analysis_cancel_prevents_late_shortlist_analysis_but_allows_plain_selection(
    make_bridge, needs_analysis,
):
    bridge = make_bridge()
    selections, analyzed = [], []
    bridge.service.shortlist_requires_analysis = lambda job: needs_analysis
    bridge.service.set_job_shortlisted = lambda job, *, selected: (
        selections.append((job, selected)) or True
    )
    bridge.service.analyze_and_shortlist = lambda ids, **kwargs: (
        analyzed.append(ids) or {"shortlisted": 1}
    )
    for lane in ("analysis", "search"):
        bridge._run(
            lane, lambda **kwargs: None,
            workflow=lane, task_key=lane, cancellable=True,
        )
    analysis_worker = bridge._active_tasks["analysis"]["worker"]
    search_worker = bridge._active_tasks["search"]["worker"]
    bridge.setJobShortlisted(42, True)
    assert bridge._shortlist_intents == {}  # Inspection has not completed yet.

    bridge.cancelTaskLane("analysis")
    bridge._thread_pool.finish(analysis_worker)
    while bridge._mutation_pool.pending:
        bridge._mutation_pool.finish_next()

    assert analyzed == []
    assert bridge._shortlist_intents == {}
    assert set(task_states(bridge)) == {"search"}
    assert bridge._thread_pool.pending == [search_worker] and not search_worker.cancel_requested
    # Selecting an already analyzed offer is a plain saved decision and needs no LLM call.
    assert selections == ([] if needs_analysis else [(42, True)])


def test_progress_and_out_of_order_completion_preserve_other_lanes(make_bridge):
    bridge = make_bridge()
    for lane in ("search", "analysis", "generation"):
        bridge._run(lane, lambda **kwargs: None, workflow=lane, task_key=lane, cancellable=True)
    workers = {lane: state["worker"] for lane, state in bridge._active_tasks.items()}
    for lane, worker in workers.items():
        worker.signals.progress.emit(f"{lane}: étape 2")

    states = task_states(bridge)
    assert {lane: state["label"] for lane, state in states.items()} == {
        lane: f"{lane}: étape 2" for lane in workers
    }
    assert bridge.searchLabel == "search: étape 2"
    bridge._thread_pool.finish(workers["analysis"])
    assert bridge.busy and bridge.searchRunning
    assert {lane: state["label"] for lane, state in task_states(bridge).items()} == {
        "search": "search: étape 2", "generation": "generation: étape 2",
    }
    bridge._thread_pool.finish(workers["search"])
    assert bridge.busy and not bridge.searchRunning
    assert set(task_states(bridge)) == {"generation"}
    bridge._thread_pool.finish(workers["generation"])
    assert not bridge.busy and task_states(bridge) == {}


def test_same_lane_serializes_and_deduplicates_without_blocking_other_lanes(make_bridge):
    bridge = make_bridge()
    completed = []

    def call(value, *, progress, stop_requested):
        completed.append(value)

    bridge._run("search A", call, "A", workflow="search", cancellable=True)
    bridge._run("search B", call, "B", workflow="search", cancellable=True)
    bridge._run("search B", call, "B", workflow="search", cancellable=True)
    bridge._run("analysis C", call, "C", workflow="analysis", cancellable=True)
    assert len(bridge._thread_pool.pending) == 2 and bridge.queuedTaskCount == 1
    assert task_states(bridge)["search"]["queued"] == 1
    bridge._thread_pool.finish(bridge._active_tasks["analysis"]["worker"])
    assert completed == ["C"] and bridge.queuedTaskCount == 1
    bridge._thread_pool.finish(bridge._active_tasks["search"]["worker"])
    assert completed == ["C", "A"] and bridge.queuedTaskCount == 0
    bridge._thread_pool.finish_next()
    assert completed == ["C", "A", "B"] and not bridge.busy


def test_required_maintenance_remains_a_barrier_before_parallel_launch(make_bridge):
    bridge = make_bridge()
    bridge._maintenance_pending = True
    steps = []
    bridge.service.startup_maintenance = lambda **kwargs: steps.append("maintenance") or True
    for lane in ("search", "analysis", "generation"):
        bridge._run(
            lane, lambda **kwargs: steps.append("workflow"),
            workflow=lane, task_key=lane, cancellable=True,
        )

    assert len(bridge._thread_pool.pending) == 1
    assert bridge.queuedTaskCount == 3
    bridge._thread_pool.finish_next()
    assert steps == ["maintenance"]
    assert not bridge._maintenance_pending
    assert len(bridge._thread_pool.pending) == 3
    while bridge._thread_pool.pending:
        bridge._thread_pool.finish_next()
    assert steps == ["maintenance", "workflow", "workflow", "workflow"]


def test_shutdown_waits_for_every_lane_and_preserves_note_retry(make_bridge):
    bridge = make_bridge()
    attempted, cancellation = [], []

    def call(lane, *, progress, stop_requested):
        cancellation.append((lane, stop_requested()))

    def save(application_id, text):
        attempted.append((application_id, text))
        if len(attempted) == 1:
            raise RuntimeError("Temporary disk error")
        return True

    bridge.service.save_application_notes = save
    bridge.notesFlushRequested.connect(lambda: bridge.saveApplicationNotes(7, "draft", 1))
    for lane in ("search", "analysis", "generation"):
        bridge._run(lane, call, lane, workflow=lane, cancellable=True)
        bridge._run("queued", call, lane + " queued", workflow=lane, cancellable=True)
    bridge.requestShutdown()
    assert bridge.queuedTaskCount == 0 and not bridge.shutdownReady
    while bridge._thread_pool.pending:
        bridge._thread_pool.finish_next()
        assert not bridge.shutdownReady
    bridge._mutation_pool.finish_next()
    assert not bridge.shutdownReady
    bridge.retryNotesSave()
    while bridge._mutation_pool.pending:
        bridge._mutation_pool.finish_next()
    assert bridge.shutdownReady
    assert dict(cancellation) == {"search": True, "analysis": True, "generation": True}
    assert len(attempted) >= 2 and set(attempted) == {(7, "draft")}


def test_current_analysis_generation_controllers_and_search_really_overlap(make_bridge):
    bridge = make_bridge(real_workers=True)
    entered = {lane: Event() for lane in ("search", "analysis", "generation")}
    releases = {lane: Event() for lane in entered}
    observations = {}
    bridge.service.search_jobs = blocked_call(
        make_bridge, entered["search"], releases["search"], observations, "search",
    )
    bridge.service.start_immediate_analysis = blocked_call(
        make_bridge, entered["analysis"], releases["analysis"], observations, "analysis",
        {"workspace": {"candidates": [], "runs": [], "active_run_id": ""},
         "report": SimpleNamespace(analyzed=1, errors=[], screened_out=0, cancelled=False)},
    )
    bridge.service.start_immediate_generation = blocked_call(
        make_bridge, entered["generation"], releases["generation"], observations, "generation",
        {"runs": [], "active_run_id": ""},
    )

    bridge.searchJobs("AI Engineer", "France", ["wttj"], 20, "week")
    bridge.startImmediateAnalysis([41])
    bridge.startImmediateGeneration([42])
    wait_until(lambda: all(event.is_set() for event in entered.values()))
    assert set(task_states(bridge)) == {"search", "analysis", "generation"}
    assert bridge.queuedTaskCount == 0
    bridge.cancelTaskLane("analysis")
    assert bridge._analysis_controller._immediate_worker.cancel_requested
    assert not bridge._generation_controller._command_worker.cancel_requested
    assert not bridge._active_tasks["search"]["worker"].cancel_requested
    releases["analysis"].set()
    wait_until(lambda: "analysis" not in task_states(bridge))
    assert set(task_states(bridge)) == {"search", "generation"}
    assert bridge.searchRunning
    releases["search"].set()
    wait_until(lambda: not bridge.searchRunning)
    assert set(task_states(bridge)) == {"generation"}
    releases["generation"].set()
    wait_until(lambda: not bridge.busy)
    assert observations == {"analysis": True, "search": False, "generation": False}
