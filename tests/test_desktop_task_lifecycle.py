"""Deterministic scheduling, refresh and shutdown contracts for the Qt bridge."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QAbstractItemModelTester

from smartapply.desktop.bridge import DesktopBridge, _enrich_rows
from smartapply.desktop.models import OfferListModel


class ControlledPool:
    def __init__(self):
        self.pending = []

    def start(self, worker):
        self.pending.append(worker)

    def finish_next(self):
        self.pending.pop(0).run()


def test_deletion_clears_detail_and_failed_note_drafts(bridge, monkeypatch):
    from smartapply.desktop.offer_deletion import OfferDeletionResult

    bridge.service.delete_ready_offer = lambda job_id: OfferDeletionResult((42, 43), (7,), True)
    bridge._selected_job_id = 42
    bridge._current_job = {"id": 42, "application": {"id": 7}}
    bridge._notes_failures[7] = ("old draft", 1)
    bridge._notes_latest[7] = 1
    notifications = []
    bridge.offersDeleted.connect(lambda *args: notifications.append(args))
    monkeypatch.setattr(bridge, "_refresh_workflow", lambda: None)
    bridge.deleteReadyOffer(42)
    bridge._mutation_pool.finish_next()
    assert not bridge.currentJob.get("id") and bridge._selected_job_id == 0
    assert 7 not in bridge._notes_failures and 7 not in bridge._notes_latest
    assert notifications == [([42, 43], [7])]


def test_notes_queued_before_deletion_cannot_write_into_a_reused_id(bridge, monkeypatch):
    from smartapply.desktop.offer_deletion import OfferDeletionResult

    writes = []
    bridge.service.save_application_notes = lambda *args: writes.append(args) or True
    bridge.service.delete_ready_offer = lambda job_id: OfferDeletionResult((42,), (7,), True)
    monkeypatch.setattr(bridge, "_refresh_workflow", lambda: None)
    bridge.deleteReadyOffer(42)
    bridge.saveApplicationNotes(7, "old draft", 1)
    bridge._mutation_pool.finish_next()
    bridge._mutation_pool.finish_next()
    assert writes == [] and not bridge._notes_failures
    bridge.saveApplicationNotes(7, "new dossier draft", 1)
    bridge._mutation_pool.finish_next()
    assert writes == [(7, "new dossier draft")]


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
    value._busy_jobs_controller.stop()
    assert value._busy_jobs_controller._pool.waitForDone(3_000)
    value._unsubscribe_resources()
    value._unsubscribe_database()
    value._workflow_refresh_timer.stop()
    application.processEvents()


def test_default_search_query_covers_all_families_without_profile_mutation(bridge):
    from smartapply.pipeline.ingest.role_families import ROLE_FAMILIES, matching_role_families

    bridge._profile = {"target_roles": ["Research Engineer", "Applied Scientist"]}
    query = bridge.defaultSearchQuery

    assert {family.key for family in matching_role_families(query.split(" OR "))} == {
        family.key for family in ROLE_FAMILIES
    }
    assert query.startswith("AI Engineer OR ")
    assert " OR Research Engineer OR " not in query
    assert " OR Applied Scientist OR " not in query
    assert bridge.profile["target_roles"] == ["Research Engineer", "Applied Scientist"]


@pytest.mark.parametrize(
    ("disposition", "label"),
    [
        ("rejected", "Rejeté"),
        ("pending_relevance", "À classer"),
        ("relevant", "À classer"),
        ("uncertain", "À classer"),
        ("", ""),
        (None, ""),
    ],
)
def test_offer_rows_only_display_the_two_current_class_labels(disposition, label):
    row = {"id": 42, "title": "ML Engineer", "filter_disposition": disposition}

    enriched = _enrich_rows([row])[0]

    assert enriched["filter_disposition_label"] == label
    # Labels do not change the stored decision or make decisions in the UI.
    assert enriched["filter_disposition"] == disposition
    assert row["filter_disposition"] == disposition


@pytest.mark.parametrize("same_offer", [True, False])
@pytest.mark.parametrize("outcome", [True, False, "error"])
def test_duplicate_completion_reports_only_persisted_success(bridge, monkeypatch, same_offer, outcome):
    completions, refreshed = [], []

    def resolve(job_id, *, same_offer):
        if outcome == "error":
            raise RuntimeError("Persistence failed")
        return outcome

    bridge.service.resolve_duplicate = resolve
    bridge.duplicateResolutionFinished.connect(lambda *args: completions.append(args))
    monkeypatch.setattr(bridge, "_refresh_workflow", lambda: refreshed.append("list"))
    monkeypatch.setattr(bridge, "_refresh_selected", lambda job: refreshed.append(job))

    bridge.resolveDuplicate(42, same_offer)
    assert completions == []
    bridge._mutation_pool.finish_next()

    assert completions == [(42, outcome is True)]
    assert refreshed == (["list", 42] if outcome is True else [])


def test_clearing_last_duplicate_selection_discards_late_detail(bridge):
    bridge.service.get_job = lambda job: {"id": job}
    bridge.selectJob(42)
    assert bridge.jobDetailLoading

    bridge.selectJob(0)
    bridge._read_pool.finish_next()

    assert bridge._selected_job_id == 0
    assert not bridge.currentJob.get("id") and not bridge.jobDetailLoading


def test_passive_refresh_preserves_explicitly_cleared_duplicate_selection(bridge):
    bridge.service.get_job = lambda job: {"id": job}
    bridge.service.list_jobs = lambda **kwargs: [{"id": 99, "title": "Outside the page filter"}]
    bridge._selected_job_id = 42
    bridge.loadJobs("", "duplicate_review", "duplicate_confidence", False)
    bridge._read_pool.finish_next()
    bridge.selectJob(0)

    bridge.refreshJobs()
    bridge._read_pool.finish_next()

    assert not bridge.currentJob.get("id") and not bridge._read_pool.pending
    # Explicit navigation to a different list can select its first offer.
    bridge.loadJobs("", "scraped", "title", False)
    bridge._read_pool.finish_next()
    bridge._read_pool.finish_next()
    assert bridge.currentJob["id"] == 99


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


def test_passive_refresh_keeps_requested_page_navigation(bridge):
    pages = []

    def read(**kwargs):
        page = kwargs["page"]
        pages.append(page)
        return {"rows": [{"id": page + 1, "title": f"page {page}"}],
                "total": 200, "page": page}

    bridge.service.list_jobs_page = read
    bridge._selected_job_id = 999
    bridge.loadJobs("", "analyzed", "title", True, "", 0)
    bridge._read_pool.finish_next()
    assert bridge.jobsPage == 0

    bridge.loadJobs("", "analyzed", "title", True, "", 1)
    assert bridge.jobsPage == 0 and bridge._requested_jobs_page == 1
    bridge.refreshJobs()
    bridge._read_pool.finish_next()

    assert pages == [0, 1]
    assert bridge.jobsPage == 1 and bridge.jobs[0]["title"] == "page 1"
    bridge.refreshJobs()
    bridge._read_pool.finish_next()
    assert pages == [0, 1, 1] and bridge.jobsPage == 1


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
    bridge._duplicate_pending_only = True
    bridge.loadJobs("A", "duplicate_review", "duplicate_confidence", False)
    bridge._read_pool.finish_next()
    displayed = bridge.jobs.copy()
    search, status, sort_key, ascending, pending_only = changed
    bridge._duplicate_pending_only = pending_only

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


def test_classification_filter_is_preserved_on_refresh_and_ignores_stale_result(bridge):
    seen = []

    def read(**kwargs):
        seen.append(kwargs)
        return [{"id": 1, "title": kwargs["filter_disposition"]}]

    bridge.service.list_jobs = read
    bridge._selected_job_id = 1
    bridge.loadJobs("Paris", "scraped", "title", True, "pending_relevance")
    bridge._read_pool.finish_next()
    assert bridge.jobs[0]["title"] == "pending_relevance"
    bridge.loadJobs("Paris", "scraped", "title", True, "rejected")
    bridge.loadJobs("Paris", "scraped", "title", True, "pending_relevance")
    bridge._read_pool.finish_next()
    assert bridge.jobs[0]["title"] == "pending_relevance"
    bridge._read_pool.finish_next()
    assert bridge.jobs[0]["title"] == "pending_relevance"

    bridge.refreshJobs()
    assert not bridge.jobsLoading
    bridge._read_pool.finish_next()
    assert seen[-1]["filter_disposition"] == "pending_relevance"
    bridge.loadJobs("Paris", "archived", "title", True, "pending_relevance")
    bridge._read_pool.finish_next()
    assert seen[-1]["filter_disposition"] == ""


def test_minimum_ai_score_survives_sort_refresh_and_status_changes(bridge):
    seen = []

    def read(**kwargs):
        seen.append(kwargs)
        return [{"id": 1, "title": str(kwargs["min_llm_score"])}]

    bridge.service.list_jobs = read
    bridge._selected_job_id = 1
    bridge.loadJobs("", "analyzed", "llm_score", False)
    bridge._read_pool.finish_next()
    assert seen[-1]["min_llm_score"] is None

    bridge.setMinimumLlmScorePercent(70)
    bridge.loadJobs("", "analyzed", "llm_score", False)
    bridge.loadJobs("", "analyzed", "operation_date", False)
    bridge._read_pool.finish_next()
    bridge._read_pool.finish_next()
    assert bridge.minimumLlmScorePercent == 70
    assert bridge.jobs[0]["title"] == "0.7"
    assert seen[-1]["min_llm_score"] == 0.7

    bridge.refreshJobs()
    bridge._read_pool.finish_next()
    assert seen[-1]["min_llm_score"] == 0.7
    selected = []
    bridge.allMatchingJobsReady.connect(lambda rows: selected.extend(rows))

    def matching(**kwargs):
        seen.append(kwargs)
        return [SimpleNamespace(
            id=1, status="analyzed", application_id=None,
            shortlisted=False, can_generate=True,
        )]

    bridge.service.list_jobs = matching
    bridge.selectAllMatchingJobs()
    bridge._read_pool.finish_next()
    assert seen[-1]["min_llm_score"] == 0.7
    assert [row["id"] for row in selected] == [1]
    bridge.service.list_jobs = read
    for status in ("ready_for_form_submission", "sent"):
        bridge.loadJobs("", status, "llm_score", False)
        bridge._read_pool.finish_next()
        assert seen[-1]["min_llm_score"] == 0.7
    bridge.loadJobs("", "archived", "score", False)
    bridge._read_pool.finish_next()
    assert seen[-1]["min_llm_score"] is None
    bridge.loadJobs("", "analyzed", "llm_score", False)
    bridge._read_pool.finish_next()
    assert seen[-1]["min_llm_score"] == 0.7

    bridge.setMinimumLlmScorePercent(0)
    bridge.refreshJobs()
    bridge._read_pool.finish_next()
    assert seen[-1]["min_llm_score"] == 0.0
    bridge.setMinimumLlmScorePercent(-1)
    bridge.refreshJobs()
    bridge._read_pool.finish_next()
    assert seen[-1]["min_llm_score"] is None


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


@pytest.mark.parametrize("action", ["archiveJob", "markJobSent", "archiveApplication", "deleteReadyOffer"])
@pytest.mark.parametrize("ids,removed,expected", [
    (list(range(1, 103)), {100}, 101),
    ([99, 100], {100}, 99),
    ([100], {100}, 0),
    ([100, 101, 102], {100, 101}, 102),
    ([100, 101], set(), 101),
])
def test_offer_action_advances_in_global_order(bridge, monkeypatch, action, ids, removed, expected):
    from smartapply.desktop.offer_deletion import OfferDeletionResult

    current = list(ids)
    filters = []
    bridge._selected_job_id = 100
    bridge._current_job = {"id": 100, "application": {"id": 7}}
    bridge._job_search = "engineer"
    bridge._job_sort_key = "llm_score"
    bridge._job_sort_ascending = False
    bridge._job_archive_origin = "manual"
    bridge._job_mission_relation = "direct"

    def listing(**kwargs):
        filters.append(kwargs)
        return list(current)

    def mutate(_):
        current[:] = [i for i in current if i not in removed]
        if action == "deleteReadyOffer":
            return OfferDeletionResult(tuple(removed), (7,), True)
        return True

    bridge.service.list_jobs = listing
    for name in ("archive_job", "mark_job_sent", "archive_application", "delete_ready_offer"):
        setattr(bridge.service, name, mutate)
    selections = []
    monkeypatch.setattr(bridge, "_refresh_workflow", lambda: None)
    monkeypatch.setattr(bridge, "_refresh_selected", lambda _: None)
    monkeypatch.setattr(bridge, "selectJob", selections.append)
    getattr(bridge, action)(7 if action == "archiveApplication" else 100)
    assert selections == []
    bridge._mutation_pool.finish_next()
    assert selections[-1] == expected
    assert len(filters) == 2 and filters[0] == filters[1]
    assert filters[0]["ids_only"] and filters[0]["search"] == "engineer"
    assert filters[0]["archive_origin"] == "manual"
    assert filters[0]["mission_relation"] == "direct"


@pytest.mark.parametrize("change", ["selection", "revision", "query", "route", "failure", "exception"])
def test_offer_action_does_not_override_navigation_or_advance_on_failure(bridge, monkeypatch, change):
    bridge._selected_job_id = 100
    bridge._requested_jobs_query = ("initial",)
    bridge.service.list_jobs = lambda **_: [100, 101]

    def mutate(_):
        if change == "exception":
            raise ValueError("refused")
        return change != "failure"

    bridge.service.mark_job_sent = mutate
    selections = []
    monkeypatch.setattr(bridge, "_refresh_workflow", lambda: None)
    monkeypatch.setattr(bridge, "_refresh_selected", lambda _: None)
    monkeypatch.setattr(bridge, "selectJob", selections.append)
    bridge.markJobSent(100)
    if change == "selection":
        bridge._selected_job_id = 102
    elif change == "revision":
        bridge._selection_revision = 2
    elif change == "query":
        bridge._requested_jobs_query = ("new",)
    elif change == "route":
        bridge._active_route = "home"
    bridge._mutation_pool.finish_next()
    assert selections == []


def test_next_offer_lookup_failure_keeps_successful_mutation(bridge, monkeypatch):
    bridge._selected_job_id = 100
    reads = []
    writes = []
    notifications = []

    def listing(**_):
        reads.append(True)
        if len(reads) == 2:
            raise RuntimeError("temporarily unavailable")
        return [100, 101]

    bridge.service.list_jobs = listing
    bridge.service.mark_job_sent = lambda job_id: writes.append(job_id) or True
    monkeypatch.setattr(bridge, "_job_action_done", lambda *args: notifications.append(args))
    monkeypatch.setattr(bridge, "selectJob", lambda _: pytest.fail("lookup failed"))
    bridge.markJobSent(100)
    bridge._mutation_pool.finish_next()
    assert writes == [100] and len(notifications) == 1
