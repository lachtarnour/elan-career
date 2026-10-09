"""Archive categories must filter the full query before paging or selection."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject

from smartapply.database import session_scope
from smartapply.database.models import Application, Job, JobScore, JobStatus
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.services import DesktopService


def _archive(session, key, components=None, *, application_only=False):
    job = Job(external_id=key, title=key, company="Fixture", source="fixture",
              description="ML", status=JobStatus.ARCHIVED if not application_only else "analyzed")
    if components is not None:
        job.score = JobScore(components=components)
    if application_only:
        job.application = Application(status=JobStatus.ARCHIVED)
    session.add(job)
    session.flush()
    return job


def test_archive_filter_matches_labels_and_precedes_pagination(isolated_db):
    with session_scope() as session:
        root = Job(external_id="root", title="Root", company="Fixture", source="fixture", description="ML")
        session.add(root)
        session.flush()
        cases = [
            ("a-manual", {"archive_origin": "manual", "rejection_stage": "local_filter"}, "manual"),
            ("b-auto", {"archive_origin": "automatic_filter"}, "automatic_filter"),
            ("c-legacy", {"rejection_stage": "rules"}, "automatic_filter"),
            ("d-mission", {"rejection_reasons": ["mission_clearly_outside"]}, "mission_screen"),
            ("e-manual", {"rejection_reasons": ["manual_archive"]}, "manual"),
            ("f-application", {"archive_origin": "manual"}, "manual"),
            ("g-unknown", None, ""),
            ("h-substring", {"rejection_reasons": ["not_manual_archive"]}, ""),
            ("h-object", {"rejection_reasons": {"cause": "manual_archive"}}, ""),
            ("i-duplicate", {"archive_origin": "manual"}, "confirmed_duplicate"),
        ]
        expected = {}
        for key, components, origin in cases:
            job = _archive(session, key, components, application_only=key == "f-application")
            if origin == "confirmed_duplicate":
                job.canonical_job_id = root.id
                job.duplicate_review_status = "confirmed"
            expected[job.id] = origin
    service = DesktopService()
    all_rows = service.list_jobs(status="archived", sort_key="title", sort_ascending=True)
    assert set(expected) == {row.id for row in all_rows}
    labels = {"automatic_filter": "Filtre automatique", "mission_screen": "Missions hors cible",
              "manual": "Archivage manuel", "confirmed_duplicate": "Doublon confirmé", "": ""}
    assert all(row.archive_sub_label == labels[expected[row.id]] for row in all_rows)
    for origin in labels.keys() - {""}:
        ids = [row.id for row in all_rows if expected[row.id] == origin]
        filters = dict(status="archived", archive_origin=origin, sort_key="title", sort_ascending=True)
        assert service.list_jobs(**filters, ids_only=True) == ids
        for index, job_id in enumerate(ids):
            page = service.list_jobs_page(**filters, page=index, page_size=1)
            assert page["total"] == len(ids)
            assert [row.id for row in page["rows"]] == [job_id]
            assert service.job_page_for(job_id, **filters, page_size=1) == index
    assert service.list_jobs(status="archived", archive_origin="manual", search="f-application")[0].title == "f-application"


@pytest.mark.parametrize("origin", ["unknown", "rejected", "not_specified"])
def test_archive_filter_rejects_unsupported_origins(isolated_db, origin):
    with pytest.raises(ValueError, match="Unsupported archive origin"):
        DesktopService().list_jobs(status="archived", archive_origin=origin)


def test_archive_filter_is_scoped_to_archived_offers(isolated_db):
    with pytest.raises(ValueError, match="only for archived offers"):
        DesktopService().list_jobs(status="analyzed", archive_origin="manual")


def test_bridge_preserves_archive_filter_for_global_selection_and_refresh():
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._shutdown_requested = False
    bridge._selection_loading = False
    bridge._job_search, bridge._job_status = "Paris", "archived"
    bridge._job_sort_key, bridge._job_sort_ascending = "title", True
    bridge._job_filter_disposition, bridge._job_archive_origin = "", "manual"
    bridge._minimum_llm_score_percent, bridge._jobs_page = -1, 3
    bridge.service = SimpleNamespace(list_jobs=lambda **kwargs: None)
    calls, emitted = [], []
    bridge._start_read = lambda *args, **kwargs: calls.append(kwargs)
    bridge.allMatchingJobsReady.connect(emitted.append)
    row = SimpleNamespace(id=1, status="archived", application_id=None, shortlisted=False,
                          can_generate=False)
    bridge.selectAllMatchingJobs()
    assert calls[-1]["archive_origin"] == "manual"
    # A late selection from another archive category must not select its offers.
    bridge._job_archive_origin = "mission_screen"
    calls[-1]["on_success"]([row])
    assert emitted == []
    calls[-1]["on_finished"]()
    bridge.selectAllMatchingJobs()
    calls[-1]["on_success"]([row])
    assert emitted[0][0]["id"] == 1
    refreshed = []
    bridge.loadJobs = lambda *args: refreshed.append(args)
    bridge.refreshJobs()
    assert refreshed == [("Paris", "archived", "title", True, "", 3, "mission_screen")]
