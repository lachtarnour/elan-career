"""Reservation reads never execute in QML getters or accumulate during bursts."""

from threading import Event, get_ident
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QCoreApplication, QTimer

from smartapply.desktop import busy_jobs_controller as reservations
from tests.test_desktop_task_lifecycle import ControlledPool
from tests.test_desktop_task_lifecycle import bridge as bridge


def wait_until(predicate):
    deadline = monotonic() + 3
    while not predicate() and monotonic() < deadline:
        QCoreApplication.processEvents()
        sleep(0.005)
    assert predicate()


@pytest.fixture
def controlled(bridge):
    controller = bridge._busy_jobs_controller
    controller._timer.stop()
    pool = ControlledPool()
    original = controller._pool
    controller._pool = pool
    yield bridge, controller, pool
    controller.stop()
    while pool.pending:
        pool.finish_next()
    controller._pool = original


def read_now(controller):
    controller._timer.stop()
    controller._refresh()


def test_property_and_slot_use_only_memory_and_return_detached_lists(controlled, monkeypatch):
    bridge, controller, pool = controlled
    calls = []
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: calls.append(1) or list(range(600)))
    read_now(controller)
    pool.finish_next()
    assert calls == [1]

    def unexpected_read():
        pytest.fail("A QML getter/slot performed a reservation database read")

    monkeypatch.setattr(reservations, "busy_job_ids", unexpected_read)
    for _ in range(107):
        assert bridge.busyJobIds == list(range(600))
        assert bridge.isJobBusy(599)
        assert not bridge.isJobBusy(601)
    detached = bridge.busyJobIds
    detached.clear()
    assert len(bridge.busyJobIds) == 600 and bridge.isJobBusy(599)


def test_burst_notifications_coalesce_and_only_changed_snapshot_notifies(controlled, monkeypatch):
    bridge, controller, pool = controlled
    calls, notifications = [], []
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: calls.append(1) or [2, 1, 2])
    bridge.busyJobIdsChanged.connect(lambda: notifications.append(bridge.busyJobIds))
    for _ in range(380):
        bridge._resources_changed()
    assert controller._timer.isActive()
    assert not pool.pending and notifications == []
    read_now(controller)
    assert len(pool.pending) == 1
    pool.finish_next()
    assert calls == [1] and notifications == [[1, 2]]
    for _ in range(380):
        bridge._resources_changed()
    read_now(controller)
    pool.finish_next()
    assert calls == [1, 1] and notifications == [[1, 2]]


def test_invalidation_retains_protections_and_keeps_one_fresh_followup(controlled, monkeypatch):
    bridge, controller, pool = controlled
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: [41])
    read_now(controller)
    for _ in range(380):
        bridge._resources_changed()
        controller._refresh()
    assert len(pool.pending) == 1
    pool.finish_next()
    assert bridge.busyJobIds == [41] and controller._timer.isActive()
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: [42])
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [42] and not controller._timer.isActive()


@pytest.mark.parametrize("topic", ["jobs", "generation_runs", "generation_items"])
def test_durable_and_alias_changes_refresh_even_outside_offers_page(controlled, topic):
    bridge, controller, pool = controlled
    bridge._active_route = "analysis"
    revision = controller._revision
    bridge._database_changed({topic})
    assert controller._revision == revision + 1 and controller._timer.isActive()
    assert not pool.pending


@pytest.mark.parametrize("topic", ["llm_usage", "generation_calls", "app_settings", "companies"])
def test_unrelated_writes_do_not_refresh_reservations(controlled, topic):
    bridge, controller, _ = controlled
    revision = controller._revision
    bridge._database_changed({topic})
    assert controller._revision == revision and not controller._timer.isActive()


def test_error_keeps_last_snapshot_and_retries_with_bounded_backoff(controlled, monkeypatch):
    bridge, controller, pool = controlled
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: [41])
    read_now(controller)
    pool.finish_next()

    def failing():
        raise RuntimeError("temporary database lock")

    monkeypatch.setattr(reservations, "busy_job_ids", failing)
    notifications = []
    bridge.busyJobIdsChanged.connect(lambda: notifications.append(bridge.busyJobIds))
    for _ in range(7):
        read_now(controller)
        pool.finish_next()
        assert bridge.busyJobIds == [41] and bridge.isJobBusy(41)
        assert 500 <= controller._timer.interval() <= 10_000
    assert notifications == [] and controller._timer.interval() == 10_000
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: [42])
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [42] and controller._retry_ms == 0
    assert notifications == [[42]] and not controller._timer.isActive()


def test_stale_error_does_not_delay_newer_snapshot(controlled, monkeypatch):
    bridge, controller, pool = controlled

    def failing():
        raise RuntimeError("obsolete read failed")

    monkeypatch.setattr(reservations, "busy_job_ids", failing)
    read_now(controller)
    bridge._resources_changed()
    pool.finish_next()
    assert controller._retry_ms == 0 and controller._timer.interval() == 25
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: [42])
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [42]


def test_new_change_expediates_retry_without_postponing_normal_coalescing(controlled, monkeypatch):
    bridge, controller, pool = controlled

    def failing():
        raise RuntimeError("temporary database lock")

    monkeypatch.setattr(reservations, "busy_job_ids", failing)
    read_now(controller)
    pool.finish_next()
    assert controller._timer.interval() == 500
    bridge._resources_changed()
    assert controller._timer.interval() == 25
    timer_id = controller._timer.timerId()
    for _ in range(380):
        bridge._resources_changed()
    assert controller._timer.timerId() == timer_id
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: [42])
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [42] and not controller._timer.isActive()


def test_shutdown_waits_for_read_but_discards_its_result(controlled, monkeypatch):
    bridge, controller, pool = controlled
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: [42])
    read_now(controller)
    bridge.requestShutdown()
    QCoreApplication.processEvents()
    assert not bridge.shutdownReady
    pool.finish_next()
    assert bridge.shutdownReady and not bridge.busyJobIds
    bridge._resources_changed()
    controller._refresh()
    assert not pool.pending and not controller._timer.isActive()


def test_real_slow_read_runs_off_gui_thread_and_leaves_event_loop_responsive(bridge, monkeypatch):
    controller = bridge._busy_jobs_controller
    controller._timer.stop()
    entered, release = Event(), Event()
    threads, callbacks, notifications = [], [], []
    gui_thread = get_ident()

    def slow_read():
        threads.append(get_ident())
        entered.set()
        assert release.wait(3)
        return [42]

    monkeypatch.setattr(reservations, "busy_job_ids", slow_read)
    bridge.busyJobIdsChanged.connect(lambda: notifications.append(get_ident()))
    read_now(controller)
    try:
        wait_until(entered.is_set)
        QTimer.singleShot(0, lambda: callbacks.append(get_ident()))
        wait_until(lambda: bool(callbacks))
        assert bridge.busyJobIds == [] and not bridge.isJobBusy(42)
        assert threads == [threads[0]] and threads[0] != gui_thread
        assert callbacks == [gui_thread] and not release.is_set()
    finally:
        release.set()
    wait_until(lambda: bridge.busyJobIds == [42])
    wait_until(lambda: controller._worker is None)
    assert notifications == [gui_thread]
