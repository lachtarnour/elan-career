"""Deterministic scheduling, refresh and shutdown contracts for the Qt bridge."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QAbstractItemModelTester

from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.models import OfferListModel


class ControlledPool:
    def __init__(self):
        self.pending = []

    def start(self, worker):
        self.pending.append(worker)

    def finish_next(self):
        self.pending.pop(0).run()


@pytest.fixture
def bridge(monkeypatch):
    application = QCoreApplication.instance() or QCoreApplication([])
    for name in (
        "refreshDashboard",
        "refreshShortlist",
        "refreshProfile",
        "refreshDiagnosticsIfStale",
    ):
        monkeypatch.setattr(DesktopBridge, name, lambda self: None)
    service = SimpleNamespace(
        initialize=lambda: None,
        analysis_top_k=lambda: 25,
        shortlist_top_k=lambda: 25,
        startup_maintenance_needed=lambda: False,
    )
    value = DesktopBridge(service)
    value._thread_pool = ControlledPool()
    value._mutation_pool = ControlledPool()
    value._read_pool = ControlledPool()
    yield value
    value._unsubscribe_resources()
    value._unsubscribe_database()
    value._workflow_refresh_timer.stop()
    application.processEvents()


def test_long_fifo_deduplicates_while_short_action_on_b_completes(bridge):
    calls = []

    def long(job, *, progress, stop_requested):
        calls.append(job)

    bridge._run("A", long, "A", cancellable=True)
    bridge._run("C", long, "C", cancellable=True)
    bridge._run("C", long, "C", cancellable=True)
    bridge._mutate(lambda: calls.append("B"))
    bridge._mutation_pool.finish_next()
    assert calls == ["B"] and bridge.busy and bridge.queuedTaskCount == 1
    bridge._thread_pool.finish_next()
    bridge._thread_pool.finish_next()
    assert calls == ["B", "A", "C"] and not bridge.busy


def test_slow_view_read_keeps_only_newest_pending_request(bridge):
    queries, shown = [], []

    def read(value):
        queries.append(value)
        return value

    for value in range(20):
        bridge._start_read("jobs", read, value, on_success=shown.append)
    assert len(bridge._read_pool.pending) == 1
    bridge._read_pool.finish_next()
    assert shown == []
    bridge._read_pool.finish_next()
    assert queries == [0, 19] and shown == [19]


def test_progress_does_not_request_sqlite_reads(bridge):
    for value in range(30):
        bridge._set_progress(str(value))
    assert bridge._read_pool.pending == []
    assert not bridge._workflow_refresh_timer.isActive()


def test_cancel_clears_queue_and_reaches_active_operation(bridge):
    cancelled = []

    def long(*, progress, stop_requested):
        cancelled.append(stop_requested())

    bridge._run("A", long, cancellable=True)
    bridge._run("B", long, cancellable=True, task_key="B")
    bridge.cancelCurrentTask()
    bridge._thread_pool.finish_next()
    assert cancelled == [True] and bridge.queuedTaskCount == 0 and not bridge.busy


def test_notes_old_retry_cannot_overwrite_a_newer_draft(bridge):
    written = []
    bridge.service.save_application_notes = lambda app, text: written.append(text) or True
    bridge._notes_failures[7] = ("old", 1)
    bridge.saveApplicationNotes(7, "new", 2)
    bridge.retryNotesSave()
    bridge._mutation_pool.finish_next()
    assert written == ["new"]
    assert bridge._notes_failures == {}


def test_shutdown_keeps_window_until_notes_retry_succeeds(bridge):
    attempts = []

    def save(app, text):
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("Disk temporarily unavailable")
        return True

    bridge.service.save_application_notes = save
    bridge.notesFlushRequested.connect(lambda: bridge.saveApplicationNotes(7, "draft", 1))
    bridge.requestShutdown()
    bridge._mutation_pool.finish_next()
    assert bridge.shutdownRequested and not bridge.shutdownReady
    bridge.retryNotesSave()
    while bridge._mutation_pool.pending:
        bridge._mutation_pool.finish_next()
    assert bridge.shutdownReady


def test_shutdown_waits_for_publication_worker_cleanup(bridge):
    published = []

    def publication(*, progress, stop_requested):
        # A critical publication section already entered completes before finished.
        published.append("atomic commit")

    bridge._run("publication", publication, cancellable=True)
    bridge.requestShutdown()
    assert not bridge.shutdownReady
    bridge._thread_pool.finish_next()
    assert published == ["atomic commit"] and bridge.shutdownReady


def test_rows_update_by_identity_without_reset_or_selection_change(bridge):
    model = OfferListModel()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Warning)
    resets = []
    model.modelReset.connect(lambda: resets.append(True))
    model.replace([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
    model.replace([{"id": 2, "title": "updated"}, {"id": 3, "title": "C"}])
    assert model.data(model.index(0), model.row_role)["title"] == "updated"
    assert model.rowCount() == 2 and resets == []
    bridge._selected_job_id = 99
    bridge._current_job = {"id": 99}
    bridge._jobs_loaded([{"id": 2, "title": "B"}])
    assert bridge.currentJob["id"] == 99
    assert tester is not None


def test_native_quit_intercepts_before_qt_destroys_workers(bridge, monkeypatch):
    from pathlib import Path

    monkeypatch.chdir(Path.cwd())
    from smartapply.desktop.app import ShutdownGuard

    application = QCoreApplication.instance()
    guard = ShutdownGuard(application, bridge)
    assert guard.eventFilter(application, QEvent(QEvent.Type.Quit))
    assert bridge.shutdownRequested
    bridge._check_shutdown()
    assert not guard.eventFilter(application, QEvent(QEvent.Type.Quit))


def test_shortlist_toggle_on_off_on_keeps_a_runnable_new_intent(bridge):
    calls = []
    bridge.service.shortlist_requires_analysis = lambda job: True
    bridge.service.analyze_and_shortlist = lambda jobs, **kwargs: (
        calls.append(jobs) or {"shortlisted": 1}
    )
    bridge.service.set_job_shortlisted = lambda job, **kwargs: True
    bridge._run("other", lambda **kwargs: None, cancellable=True)
    bridge.setJobShortlisted(5, True)
    bridge._mutation_pool.finish_next()
    bridge.setJobShortlisted(5, False)
    bridge.setJobShortlisted(5, True)
    bridge._mutation_pool.finish_next()
    bridge._thread_pool.finish_next()
    while bridge._thread_pool.pending:
        bridge._thread_pool.finish_next()
    assert calls == [[5]] and bridge._shortlist_intents == {}


def test_interrupted_maintenance_retries_before_dependent_work(bridge):
    steps = []
    bridge._maintenance_pending = True
    attempts = iter([False, True])

    def maintenance(**kwargs):
        steps.append("maintenance")
        return next(attempts)

    bridge.service.startup_maintenance = maintenance
    bridge._run("analyze", lambda **kwargs: steps.append("analysis"), cancellable=True)
    bridge._mutate(lambda: steps.append("archive B"))
    bridge._mutation_pool.finish_next()
    while bridge._thread_pool.pending:
        bridge._thread_pool.finish_next()
    assert steps == ["archive B", "maintenance", "maintenance", "analysis"]
    assert not bridge._maintenance_pending


def test_shortlist_removal_before_inspection_prevents_analysis(bridge):
    analyzed, decisions = [], []
    bridge.service.shortlist_requires_analysis = lambda job: True
    bridge.service.analyze_and_shortlist = lambda jobs, **kwargs: analyzed.append(jobs)
    bridge.service.set_job_shortlisted = lambda job, **kwargs: (
        decisions.append(kwargs["selected"]) or True
    )
    bridge.setJobShortlisted(5, True)
    bridge.setJobShortlisted(5, False)
    while bridge._mutation_pool.pending:
        bridge._mutation_pool.finish_next()
    assert analyzed == [] and decisions == [False]
    assert bridge._shortlist_intents == {}


def test_failed_maintenance_can_be_retried_without_losing_pending_task(bridge):
    bridge._maintenance_pending = True
    attempts, processed = [], []

    def maintenance(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Temporary disk error")
        return True

    bridge.service.startup_maintenance = maintenance
    bridge._run("analysis", lambda **kwargs: processed.append(True), cancellable=True)
    bridge._thread_pool.finish_next()
    assert bridge.maintenanceFailed and bridge.queuedTaskCount == 1
    bridge.retryStartupMaintenance()
    while bridge._thread_pool.pending:
        bridge._thread_pool.finish_next()
    assert processed == [True] and not bridge.maintenanceFailed


def test_company_or_preference_commit_does_not_refresh_offer_views(bridge):
    bridge._database_changed({"app_settings"})
    assert bridge._dirty_views == set()
    bridge._database_changed({"companies"})
    assert bridge._dirty_views == {"companies"}
    bridge._database_changed({"jobs"})
    assert {"jobs", "detail", "shortlist", "dashboard"} <= bridge._dirty_views


def test_late_application_open_does_not_override_a_newer_selection(bridge):
    from types import SimpleNamespace

    selected = []
    bridge.service.get_application = lambda app: SimpleNamespace(job_id=1, status="sent")
    bridge.service.get_job = lambda job: {"id": job}
    bridge.openApplication(7)
    bridge.selectJob(2)
    bridge.navigationRequested.connect(lambda *args: selected.append(args))
    while bridge._read_pool.pending:
        bridge._read_pool.finish_next()
    assert bridge.currentJob["id"] == 2 and selected == []


@pytest.mark.parametrize("rows", [[], [{"id": 1, "title": "Displayed offer"}]])
def test_refresh_of_loaded_query_keeps_content_visible_even_when_empty(bridge, rows):
    bridge.service.list_jobs = lambda **kwargs: rows
    bridge._selected_job_id = 1
    bridge.loadJobs("Python", "scraped", "published_date", False)
    assert bridge.jobsLoading
    bridge._read_pool.finish_next()
    assert not bridge.jobsLoading
    displayed = bridge.jobs.copy()

    bridge.refreshJobs()

    assert not bridge.jobsLoading and bridge.jobs == displayed
    bridge._read_pool.finish_next()
    assert not bridge.jobsLoading and bridge.jobs == displayed


@pytest.mark.parametrize(
    "changed",
    [
        ("B", "duplicate_review", "duplicate_confidence", False, True),
        ("A", "scraped", "duplicate_confidence", False, True),
        ("A", "duplicate_review", "title", False, True),
        ("A", "duplicate_review", "duplicate_confidence", True, True),
        ("A", "duplicate_review", "duplicate_confidence", False, False),
    ],
)
def test_loading_tracks_applied_query_until_new_context_is_displayed(bridge, changed):
    bridge.service.list_jobs = lambda **kwargs: [{"id": 1, "title": repr(kwargs)}]
    bridge._selected_job_id = 1
    bridge._duplicate_relevant_only = True
    bridge.loadJobs("A", "duplicate_review", "duplicate_confidence", False)
    bridge._read_pool.finish_next()
    displayed = bridge.jobs.copy()
    search, status, sort_key, ascending, relevant_only = changed
    bridge._duplicate_relevant_only = relevant_only

    bridge.loadJobs(search, status, sort_key, ascending)
    bridge.refreshJobs()

    assert bridge.jobsLoading and bridge.jobs == displayed
    bridge._read_pool.finish_next()
    assert bridge.jobsLoading and bridge.jobs == displayed
    bridge._read_pool.finish_next()
    assert not bridge.jobsLoading and bridge.jobs != displayed


def test_return_to_displayed_query_ignores_late_other_list_result(bridge):
    bridge.service.list_jobs = lambda **kwargs: [{"id": 1, "title": kwargs["search"]}]
    bridge._selected_job_id = 1
    bridge.loadJobs("A", "scraped", "title", True)
    bridge._read_pool.finish_next()
    displayed = bridge.jobs.copy()

    bridge.loadJobs("B", "scraped", "title", True)
    assert bridge.jobsLoading
    bridge.loadJobs("A", "scraped", "title", True)

    assert not bridge.jobsLoading and bridge.jobs == displayed
    bridge._read_pool.finish_next()
    assert not bridge.jobsLoading and bridge.jobs == displayed
    bridge._read_pool.finish_next()
    assert not bridge.jobsLoading and bridge.jobs == displayed


def test_offer_loading_compares_displayed_id_and_ignores_late_other_offer(bridge):
    bridge.service.get_job = lambda job: {"id": job}
    bridge._selected_job_id = 1
    bridge._current_job = {"id": 1}
    bridge.selectJob(2)
    assert bridge.jobDetailLoading and bridge.currentJob["id"] == 1
    bridge.refreshSelectedJob()
    assert bridge.jobDetailLoading and bridge.currentJob["id"] == 1

    bridge.selectJob(1)

    assert not bridge.jobDetailLoading and bridge.currentJob["id"] == 1
    bridge._read_pool.finish_next()
    assert not bridge.jobDetailLoading and bridge.currentJob["id"] == 1
    bridge._read_pool.finish_next()
    assert not bridge.jobDetailLoading and bridge.currentJob["id"] == 1


@pytest.mark.parametrize("refresh", ["direct", "selected_action", "database_change"])
def test_passive_detail_refresh_preserves_pending_application_navigation(bridge, refresh):
    routes = []
    bridge._selected_job_id = 1
    bridge._current_job = {"id": 1}
    bridge.service.get_application = lambda app: SimpleNamespace(job_id=2, status="sent")
    bridge.service.get_job = lambda job: {"id": job}
    bridge.navigationRequested.connect(lambda *args: routes.append(args))
    bridge.openApplication(7)

    if refresh == "direct":
        bridge.refreshSelectedJob()
    elif refresh == "selected_action":
        bridge._refresh_selected(1)
    else:
        bridge._active_route = "jobs"
        bridge._dirty_views.add("detail")
        bridge._flush_refresh()

    assert not bridge.jobDetailLoading and bridge.currentJob["id"] == 1
    while bridge._read_pool.pending:
        bridge._read_pool.finish_next()
    assert routes == [("jobs?status=sent", 2)]
    assert bridge.currentJob["id"] == 2 and not bridge.jobDetailLoading
