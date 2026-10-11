"""The asynchronous UI snapshot follows real reservations and duplicate groups."""

from time import monotonic
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest
from sqlalchemy import event

from smartapply.concurrency import TaskConflictError, job_operation
from smartapply.database import get_engine, write_session_scope
from smartapply.database.generation_runs import GenerationItem, GenerationRun
from smartapply.database.models import Job, JobDuplicateStatus
from smartapply.desktop.bridge import DesktopBridge


def _wait_until(predicate):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    pytest.fail("The reservation snapshot did not settle")


def _assert_snapshot(bridge, expected):
    controller = bridge._busy_jobs_controller
    _wait_until(lambda: controller._worker is None and not controller._timer.isActive())
    assert bridge.busyJobIds == sorted(expected)
    for job_id in expected:
        assert bridge.isJobBusy(job_id)


@pytest.fixture
def make_busy_bridge(isolated_db, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    for name in (
        "refreshDashboard",
        "refreshShortlist",
        "refreshProfile",
        "refreshDiagnosticsIfStale",
    ):
        monkeypatch.setattr(DesktopBridge, name, lambda self: None)
    bridges = []

    def make():
        service = SimpleNamespace(
            initialize=lambda: None,
            analysis_top_k=lambda: 25,
            shortlist_top_k=lambda: 25,
            startup_maintenance_needed=lambda: False,
        )
        bridge = DesktopBridge(service)
        # Generation reservations must refresh on the analysis page too.
        bridge._active_route = "analysis"
        bridges.append(bridge)
        return bridge

    yield make
    for bridge in bridges:
        bridge.requestShutdown()
        _wait_until(lambda bridge=bridge: bridge.shutdownReady)
        bridge._busy_jobs_controller._pool.waitForDone(5_000)
    app.processEvents()


def _jobs(*, linked=True):
    with write_session_scope() as session:
        original = Job(
            external_id="original",
            title="AI Engineer",
            source_company="Acme",
            description="Build models",
            source="manual",
        )
        other = Job(
            external_id="other",
            title="AI Engineer",
            source_company="Other",
            description="Build models",
            source="manual",
        )
        session.add_all([original, other])
        session.flush()
        alias = Job(
            external_id="alias",
            title=original.title,
            source_company=original.source_company,
            description=original.description,
            source="manual",
            canonical_job_id=original.id if linked else None,
            duplicate_review_status=JobDuplicateStatus.CONFIRMED,
        )
        session.add(alias)
        session.flush()
        probable = Job(
            external_id="probable",
            title=original.title,
            source_company=original.source_company,
            description=original.description,
            source="manual",
            possible_duplicate_of_id=alias.id,
        )
        session.add(probable)
        session.flush()
        return original.id, alias.id, probable.id, other.id


def _reservation(job_id, *, run_state="active", item_state="pending"):
    with write_session_scope() as session:
        session.add(
            GenerationRun(
                id="busy-run",
                mode="immediate",
                state=run_state,
                profile_data={},
                routes={},
                reference_date="2026-10-11",
            )
        )
        session.flush()
        item = GenerationItem(
            run_id="busy-run",
            job_id=job_id,
            state=item_state,
            job_snapshot="snapshot",
            analysis_snapshot="snapshot",
            title="AI Engineer",
            company="Acme",
        )
        session.add(item)
        session.flush()
        return item.id


@pytest.mark.parametrize(
    "run_state", ["active", "attention", "blocked", "paused", "partial", "failed"]
)
def test_startup_loads_durable_reservations_and_transitive_aliases(make_busy_bridge, run_state):
    original, alias, probable, other = _jobs()
    _reservation(original, run_state=run_state, item_state="waiting")
    bridge = make_busy_bridge()

    _assert_snapshot(bridge, [original, alias, probable])
    assert not bridge.isJobBusy(other)


def test_run_and_item_commit_notifications_update_cache_without_generation_page(make_busy_bridge):
    original, alias, probable, _ = _jobs()
    bridge = make_busy_bridge()
    _assert_snapshot(bridge, [])
    item_id = _reservation(original)
    expected = [original, alias, probable]
    _assert_snapshot(bridge, expected)

    with write_session_scope() as session:
        session.get(GenerationRun, "busy-run").cancel_requested = True
    _assert_snapshot(bridge, expected)
    for state in ("paused", "failed", "partial"):
        with write_session_scope() as session:
            session.get(GenerationRun, "busy-run").state = state
        _assert_snapshot(bridge, expected)
    for terminal in ("completed", "stale", "transferred"):
        with write_session_scope() as session:
            session.get(GenerationItem, item_id).state = terminal
        _assert_snapshot(bridge, [])
        with write_session_scope() as session:
            session.get(GenerationItem, item_id).state = "failed"
        _assert_snapshot(bridge, expected)
    for state in ("completed", "cancelled"):
        with write_session_scope() as session:
            session.get(GenerationRun, "busy-run").state = state
        _assert_snapshot(bridge, [])
        with write_session_scope() as session:
            session.get(GenerationRun, "busy-run").state = "active"
        _assert_snapshot(bridge, expected)
    with write_session_scope() as session:
        session.delete(session.get(GenerationItem, item_id))
    _assert_snapshot(bridge, [])
    with write_session_scope() as session:
        session.delete(session.get(GenerationRun, "busy-run"))
    _assert_snapshot(bridge, [])


def test_jobs_only_notifications_refresh_alias_links(make_busy_bridge):
    original, alias, probable, _ = _jobs(linked=False)
    _reservation(original, run_state="paused")
    bridge = make_busy_bridge()
    _assert_snapshot(bridge, [original])

    with write_session_scope() as session:
        session.get(Job, alias).canonical_job_id = original
    _assert_snapshot(bridge, [original, alias, probable])
    with write_session_scope() as session:
        session.get(Job, alias).canonical_job_id = None
    _assert_snapshot(bridge, [original])


def test_local_reservation_notifications_refresh_and_release_group(make_busy_bridge):
    original, alias, probable, _ = _jobs()
    bridge = make_busy_bridge()
    _assert_snapshot(bridge, [])

    with job_operation([original], operation="test analysis"):
        _assert_snapshot(bridge, [original, alias, probable])
    _assert_snapshot(bridge, [])


def test_backend_guard_rejects_alias_before_first_async_snapshot(make_busy_bridge):
    original, alias, probable, _ = _jobs()
    _reservation(original, run_state="paused")
    bridge = make_busy_bridge()
    assert bridge.busyJobIds == []

    with pytest.raises(TaskConflictError), job_operation([alias], operation="archive"):
        pytest.fail("A pending UI snapshot must never bypass durable reservations")
    _assert_snapshot(bridge, [original, alias, probable])


def test_qml_getters_issue_no_sql_after_real_reservation_load(make_busy_bridge):
    original, alias, probable, other = _jobs()
    _reservation(original, run_state="paused")
    bridge = make_busy_bridge()
    expected = sorted([original, alias, probable])
    _assert_snapshot(bridge, expected)
    statements = []
    engine = get_engine()

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        for _ in range(100):
            assert bridge.busyJobIds == expected
            assert bridge.isJobBusy(alias)
            assert not bridge.isJobBusy(other)
        # QML receives a copy and cannot erase the controller's protection.
        bridge.busyJobIds.clear()
        assert bridge.busyJobIds == expected
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert statements == []
