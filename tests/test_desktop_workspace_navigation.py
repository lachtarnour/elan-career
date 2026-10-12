"""Bounded journal reads cannot replace newer navigation or stop off-page monitoring."""

from copy import deepcopy

import pytest
from PySide6.QtCore import QCoreApplication

from tests.test_desktop_workflow_concurrency import make_bridge as make_bridge


def view_result(kind, options, *, candidates=True, monitored=None):
    selected_id = options.get("selected_run_id") or "selected"
    result = {
        "desktop_view": True,
        "view_revision": options["view_revision"],
        "history_page": options["history_page"],
        "history_page_size": 20,
        "history_total_count": 100,
        "runs": [{"id": "history", "status": "completed", "is_active": False}],
        "selected_run": {"id": selected_id, "status": "completed", "is_active": False},
        "monitor_runs": deepcopy(monitored or []),
        "polling_run_count": sum(bool(run.get("is_active")) for run in (monitored or [])),
        "reserved_job_ids": [],
    }
    if kind == "analysis" and candidates:
        result.update(
            candidates=[{"id": 42, "title": options["candidate_query"] or "AI Engineer"}],
            candidate_ids=[42, 43, 701],
            matching_candidate_ids=[42, 701],
            candidates_count=3,
            candidate_count=2,
            candidate_page=options["candidate_page"],
            candidate_page_size=100,
            candidate_revision=options["candidate_revision"],
        )
    return result


def install_reader(bridge, kind, calls, *, monitored=None):
    def read(**options):
        calls.append(deepcopy(options))
        return view_result(
            kind, options, candidates=options["include_candidates"], monitored=monitored
        )

    setattr(bridge.service, f"{kind}_workspace_view", read)
    return getattr(bridge, f"_{kind}_controller")


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_newer_navigation_rejects_old_read_and_coalesces_one_current_read(make_bridge, kind):
    bridge = make_bridge()
    calls, notifications = [], []
    controller = install_reader(bridge, kind, calls)
    controller.changed.connect(lambda: notifications.append(deepcopy(controller.workspace)))
    controller.refresh(sync=False)
    pool = controller._pool if kind == "analysis" else controller._monitor_pool
    initial = deepcopy(controller.workspace)
    assert len(pool.pending) == 1
    if kind == "analysis":
        bridge.setAnalysisCandidateQuery("Python")
        bridge.setAnalysisCandidateSort("experience", True)
        bridge.setAnalysisCandidatePage(3)
        bridge.setAnalysisHistoryPage(4)
        bridge.selectAnalysisRun("old-analysis")
    else:
        bridge.setGenerationHistoryPage(4)
        bridge.selectGenerationRun("old-generation")
    assert len(pool.pending) == 1
    pool.finish_next()
    assert controller.workspace == initial and notifications == []
    QCoreApplication.processEvents()
    assert len(pool.pending) == 1
    current_options = deepcopy(pool.pending[0].kwargs)
    assert current_options["history_page"] == 4
    assert current_options["selected_run_id"] == f"old-{kind}"
    if kind == "analysis":
        assert current_options["candidate_query"] == "Python"
        assert current_options["candidate_sort"] == "experience"
        assert current_options["candidate_ascending"] is True
        assert current_options["candidate_page"] == 3
    pool.finish_next()
    QCoreApplication.processEvents()
    assert len(calls) == 2 and not pool.pending
    assert len(notifications) == 1
    assert controller.workspace["selected_run"]["id"] == f"old-{kind}"
    assert controller.workspace["history_page"] == 4
    if kind == "analysis":
        assert "candidates" not in bridge.analysisWorkspace
        assert bridge.analysisCandidates == [{"id": 42, "title": "Python"}]


def test_progress_keeps_candidate_data_and_signal_silent(make_bridge):
    bridge = make_bridge()
    calls, notifications = [], []
    controller = install_reader(bridge, "analysis", calls)
    bridge.analysisCandidatesChanged.connect(
        lambda: notifications.append(deepcopy(bridge.analysisCandidates))
    )
    controller.refresh(sync=False)
    controller._pool.finish_next()
    original = bridge.analysisCandidates
    assert notifications == [[{"id": 42, "title": "AI Engineer"}]]
    for _ in range(20):
        controller.refresh(sync=False)
        assert controller._pool.pending[0].kwargs["include_candidates"] is False
        controller._pool.finish_next()
    assert bridge.analysisCandidates is original
    assert len(notifications) == 1 and len(calls) == 21
    assert bridge.analysisWorkspace["candidate_ids"] == [42, 43, 701]
    assert bridge.analysisWorkspace["matching_candidate_ids"] == [42, 701]


def test_stale_candidate_invalidation_keeps_page_until_one_current_reread(make_bridge):
    bridge = make_bridge()
    calls, notifications = [], []
    controller = install_reader(bridge, "analysis", calls)
    bridge.analysisCandidatesChanged.connect(
        lambda: notifications.append(deepcopy(bridge.analysisCandidates))
    )
    controller.refresh(sync=False)
    controller.invalidate_candidates()
    controller.refresh(sync=False)
    controller._pool.finish_next()
    assert bridge.analysisCandidates == [] and notifications == []
    QCoreApplication.processEvents()
    assert len(controller._pool.pending) == 1
    assert controller._pool.pending[0].kwargs["include_candidates"] is True
    assert controller._pool.pending[0].kwargs["candidate_revision"] == 1
    controller._pool.finish_next()
    QCoreApplication.processEvents()
    assert len(calls) == 2 and len(notifications) == 1 and not controller._pool.pending
    assert bridge.analysisCandidates == [{"id": 42, "title": "AI Engineer"}]


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_automatic_selection_is_pinned_during_passive_refresh_and_history_paging(make_bridge, kind):
    bridge = make_bridge()
    calls, default_run, removed = [], ["original-active"], set()

    def read(**options):
        calls.append(deepcopy(options))
        chosen = options["selected_run_id"]
        chosen = chosen if chosen and chosen not in removed else default_run[0]
        result = view_result(kind, options, candidates=options["include_candidates"])
        result["selected_run"]["id"] = chosen
        return result

    setattr(bridge.service, f"{kind}_workspace_view", read)
    controller = getattr(bridge, f"_{kind}_controller")
    pool = controller._pool if kind == "analysis" else controller._monitor_pool
    controller.refresh(sync=False)
    pool.finish_next()
    assert controller._view["selected_run_id"] == "original-active"
    default_run[0] = "new-active"
    controller.refresh(sync=False)
    pool.finish_next()
    assert calls[-1]["selected_run_id"] == "original-active"
    assert controller.workspace["selected_run"]["id"] == "original-active"
    getattr(bridge, f"set{kind.title()}HistoryPage")(4)
    assert pool.pending[0].kwargs["selected_run_id"] == "original-active"
    pool.finish_next()
    assert controller.workspace["history_page"] == 4
    assert controller.workspace["selected_run"]["id"] == "original-active"
    removed.add("original-active")
    controller.refresh(sync=False)
    pool.finish_next()
    assert controller.workspace["selected_run"]["id"] == "new-active"
    assert controller._view["selected_run_id"] == "new-active"


@pytest.mark.parametrize("navigate_during_command", [False, True])
@pytest.mark.parametrize("command", ["immediate", "retry"])
def test_completed_generation_command_is_notified_even_without_an_active_observation(
    make_bridge, navigate_during_command, command
):
    bridge = make_bridge()
    calls, notifications = [], []
    controller = install_reader(bridge, "generation", calls)
    controller._view["selected_run_id"] = "older-selection"

    run_id = f"new-{command}"

    def completed(ids, *, view, stop_requested):
        result = view_result("generation", view, candidates=False)
        result["started_run_id"] = run_id
        result["selected_run"] = {
            "id": run_id,
            "status": "completed",
            "is_active": False,
            "generated": 1,
            "failed": 0,
        }
        result["runs"] = [dict(result["selected_run"])]
        assert result["monitor_runs"] == []
        return result

    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    if command == "immediate":
        bridge.service.start_immediate_generation = completed
        bridge.startImmediateGeneration([42])
    else:
        bridge.service.retry_generation_run = completed
        bridge.retryGenerationRun(run_id)
    expected_selection = "" if command == "immediate" else "older-selection"
    assert controller._pool.pending[0].kwargs["view"]["selected_run_id"] == expected_selection
    if navigate_during_command:
        bridge.selectGenerationRun("user-selection")
    controller._pool.finish_next()
    assert len(notifications) == 1 and notifications[0][0] == "Création terminée"
    assert notifications[0][1] == "1 candidature créée."
    QCoreApplication.processEvents()
    assert len(controller._monitor_pool.pending) == 1
    if navigate_during_command:
        assert controller._monitor_pool.pending[0].kwargs["selected_run_id"] == "user-selection"
    else:
        assert controller.workspace["selected_run"]["id"] == run_id
        assert controller._view["selected_run_id"] == run_id
    controller._monitor_pool.finish_next()
    QCoreApplication.processEvents()
    if navigate_during_command:
        # Navigation started this read before command completion. The command
        # revision rejects it, then coalesces one read of the final state.
        assert len(controller._monitor_pool.pending) == 1
        assert controller._monitor_pool.pending[0].kwargs["selected_run_id"] == "user-selection"
        controller._monitor_pool.finish_next()
        QCoreApplication.processEvents()
    assert not controller._monitor_pool.pending and len(notifications) == 1


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_active_run_outside_history_page_keeps_monitoring_and_notifies_terminal_once(
    make_bridge, kind
):
    bridge = make_bridge()
    calls, notifications = [], []
    active = [
        {
            "id": "active-outside-page",
            "is_active": True,
            "status": "in_progress",
            "analyzed": 0,
            "generated": 0,
            "failed": 0,
        }
    ]
    controller = install_reader(bridge, kind, calls, monitored=active)
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    controller._view["history_page"] = 4
    controller.refresh(sync=False)
    pool = controller._pool if kind == "analysis" else controller._monitor_pool
    pool.finish_next()
    assert controller._timer.isActive() and notifications == []
    assert [run["id"] for run in controller.workspace["runs"]] == ["history"]
    assert controller._view_options()["monitor_run_ids"] == ["active-outside-page"]
    active[0] = {
        **active[0],
        "is_active": False,
        "status": "completed",
        "analyzed": 3,
        "generated": 3,
    }
    controller.refresh(sync=False)
    assert pool.pending[0].kwargs["monitor_run_ids"] == ["active-outside-page"]
    pool.finish_next()
    assert not controller._timer.isActive()
    assert len(notifications) == 1
    assert notifications[0][0] == (
        "Analyse Batch terminée" if kind == "analysis" else "Création terminée"
    )
    assert controller.workspace["history_page"] == 4
    assert controller.workspace["selected_run"]["id"] == "selected"
    controller.refresh(sync=False)
    pool.finish_next()
    assert len(notifications) == 1


@pytest.mark.parametrize("previous_status", ["active", "cancelling"])
def test_analysis_cancellation_outside_history_page_emits_one_info_notice(
    make_bridge,
    previous_status,
):
    bridge = make_bridge()
    calls, notifications = [], []
    monitored = [
        {
            "id": "cancelled-outside-page",
            "status": previous_status,
            "is_active": True,
            "analyzed": 0,
            "failed": 0,
        }
    ]
    controller = install_reader(bridge, "analysis", calls, monitored=monitored)
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    controller._view["history_page"] = 4
    controller.refresh(sync=False)
    controller._pool.finish_next()
    assert controller._timer.isActive() and notifications == []
    assert all(run["id"] != monitored[0]["id"] for run in controller.workspace["runs"])
    monitored[0] = {**monitored[0], "status": "cancelled", "is_active": False}
    cancelled = view_result("analysis", controller._view_options(), monitored=monitored)
    controller._cancel_loaded(cancelled, monitored[0]["id"])
    assert notifications == [
        (
            "Batch annulé",
            "Les offres restantes sont à nouveau disponibles.",
            "info",
        )
    ]
    assert not controller._timer.isActive()
    assert controller.workspace["history_page"] == 4
    assert controller.workspace["selected_run"]["id"] == "selected"
    controller._cancel_loaded(cancelled, monitored[0]["id"])
    controller.refresh(sync=False)
    controller._pool.finish_next()
    assert len(notifications) == 1
    assert all(kind != "success" for _title, _message, kind in notifications)
