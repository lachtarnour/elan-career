"""Conservative snapshots remain useful while every read is invalidated."""

from smartapply.desktop import busy_jobs_controller as reservations
from tests.test_desktop_busy_jobs import controlled as controlled
from tests.test_desktop_busy_jobs import read_now
from tests.test_desktop_task_lifecycle import bridge as bridge


def test_continuous_updates_publish_protection_without_accumulating_reads(controlled, monkeypatch):
    bridge, controller, pool = controlled
    calls, notifications = [], []
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: calls.append(1) or [42])
    bridge.busyJobIdsChanged.connect(lambda: notifications.append(bridge.busyJobIds))

    for iteration in range(250):
        read_now(controller)
        assert len(pool.pending) == 1 and len(bridge._read_workers) == 1
        if iteration % 2:
            bridge._database_changed({"jobs"})
        else:
            bridge._resources_changed()
        # A newer notification cannot add a concurrent read or prevent the
        # observed reservation from becoming available to the UI guards.
        controller._refresh()
        assert len(pool.pending) == 1
        pool.finish_next()
        assert bridge.busyJobIds == [42] and bridge.isJobBusy(42)
        assert controller._timer.isActive() and controller._timer.interval() == 25
        assert not pool.pending and not bridge._read_workers

    assert len(calls) == 250 and notifications == [[42]]
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [42] and not controller._timer.isActive()


def test_outdated_reads_only_add_ids_until_a_current_read_can_release_them(controlled, monkeypatch):
    bridge, controller, pool = controlled
    observed = [41]
    notifications = []
    monkeypatch.setattr(reservations, "busy_job_ids", lambda: list(observed))
    bridge.busyJobIdsChanged.connect(lambda: notifications.append(bridge.busyJobIds))
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [41]

    observed[:] = [42]
    read_now(controller)
    bridge._resources_changed()
    pool.finish_next()
    assert bridge.busyJobIds == [41, 42]

    # A freed offer can remain conservatively blocked while updates continue.
    # Neither stale empty results nor stale subsets may remove protections.
    for ids in ([], [41], [42], []):
        observed[:] = ids
        read_now(controller)
        bridge._resources_changed()
        pool.finish_next()
        assert bridge.busyJobIds == [41, 42]
        assert bridge.isJobBusy(41) and bridge.isJobBusy(42)
    assert notifications == [[41], [41, 42]]

    # Once a read covers the current revision, completed offers are released.
    observed[:] = [42]
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [42] and not bridge.isJobBusy(41)
    assert not controller._timer.isActive()
    observed.clear()
    read_now(controller)
    pool.finish_next()
    assert bridge.busyJobIds == [] and not bridge.isJobBusy(42)
    assert notifications == [[41], [41, 42], [42], []]
