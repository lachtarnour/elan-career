"""Continuous passive reads cannot postpone native provider polling."""

from copy import deepcopy

import pytest

from tests.test_desktop_workflow_concurrency import make_bridge as make_bridge


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_passive_reads_and_transient_errors_keep_the_running_poll_deadline(make_bridge, kind):
    bridge = make_bridge()
    controller = getattr(bridge, f"_{kind}_controller")
    active = {"runs": [{"id": "active", "status": "in_progress", "is_active": True}],
              "polling_run_count": 1}
    controller._loaded(active)
    timer_id = controller._timer.timerId()
    assert timer_id >= 0 and controller._timer.interval() == 120_000
    for count in range(50):
        progress = deepcopy(active)
        progress["runs"][0]["analyzed"] = count
        controller._loaded(progress)
        assert controller._timer.timerId() == timer_id
    controller._failed("Transient read error", "")
    assert controller._timer.timerId() == timer_id
    # A stopped timer still recovers after an error while an active run exists.
    controller._timer.stop()
    controller._failed("Another transient read error", "")
    assert controller._timer.isActive() and controller._timer.interval() == 120_000
    controller._loaded({"runs": [], "polling_run_count": 0})
    assert not controller._timer.isActive()


def test_generation_cadence_changes_only_when_entering_or_leaving_a_command(make_bridge):
    bridge = make_bridge()
    controller = bridge._generation_controller
    controller.workspace = {"polling_run_count": 1}
    controller._update_timer()
    idle_id = controller._timer.timerId()
    controller._command_worker = object()
    controller._update_timer()
    command_id = controller._timer.timerId()
    assert command_id != idle_id and controller._timer.interval() == 1_000
    for _ in range(20):
        controller._update_timer()
        assert controller._timer.timerId() == command_id
    controller._command_worker = None
    controller._update_timer()
    resumed_id = controller._timer.timerId()
    assert resumed_id != command_id and controller._timer.interval() == 120_000
    for _ in range(20):
        controller._update_timer()
        assert controller._timer.timerId() == resumed_id
    bridge._shutdown_requested = True
    controller._update_timer()
    assert not controller._timer.isActive()
