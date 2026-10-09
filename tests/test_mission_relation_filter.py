"""The first LLM call's mission relation filters analyzed offers globally."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject

from smartapply.database import session_scope
from smartapply.database.fit_ranking import current_policy_fingerprint
from smartapply.database.models import Job, JobAnalysis, JobScore, JobStatus
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.services import DesktopService


def test_mission_relation_filters_before_pagination_and_keeps_missing_history(isolated_db):
    expected = {}
    with session_scope() as session:
        for index, relation in enumerate(
            ("direct", "adjacent", "direct", "clearly_outside", "unknown", None)
        ):
            job = Job(
                external_id=f"mission-{index}",
                title=f"Offer {index}",
                company="Fixture",
                source="fixture",
                description="ML",
                status=JobStatus.ANALYZED,
                analyzed_at=datetime.now(timezone.utc),
            )
            job.analysis = JobAnalysis(
                fit_score=0.8 if index != 2 else 0.4,
                fit_diagnostics={"policy_fingerprint": current_policy_fingerprint()},
            )
            if relation is not None:
                job.score = JobScore(components={
                    "extraction_screen": {
                        "mission_relation": relation,
                        "mission_evidence": "sufficient",
                    }
                })
            session.add(job)
            session.flush()
            expected[job.id] = relation

    service = DesktopService()
    base = dict(status=JobStatus.ANALYZED, sort_key="title", sort_ascending=True)
    all_rows = service.list_jobs(**base)
    assert {row.id for row in all_rows} == set(expected)
    for relation in ("direct", "adjacent", "clearly_outside", "unknown"):
        ids = [row.id for row in all_rows if expected[row.id] == relation]
        filters = {**base, "mission_relation": relation}
        assert service.list_jobs(**filters, ids_only=True) == ids
        for index, job_id in enumerate(ids):
            page = service.list_jobs_page(**filters, page=index, page_size=1)
            assert page["total"] == len(ids)
            assert [row.id for row in page["rows"]] == [job_id]
            assert service.job_page_for(job_id, **filters, page_size=1) == index
    assert [row.id for row in service.list_jobs(
        **base, mission_relation="direct", min_llm_score=0.7
    )] == [next(job_id for job_id, relation in expected.items() if relation == "direct")]


@pytest.mark.parametrize("relation", ["outside", "DIRECT", "pending_relevance"])
def test_mission_relation_rejects_unsupported_values(isolated_db, relation):
    with pytest.raises(ValueError, match="Unsupported mission relation"):
        DesktopService().list_jobs(status=JobStatus.ANALYZED, mission_relation=relation)


def test_mission_relation_is_analyzed_only(isolated_db):
    with pytest.raises(ValueError, match="only for analyzed offers"):
        DesktopService().list_jobs(status=JobStatus.ARCHIVED, mission_relation="direct")


def test_bridge_preserves_mission_relation_for_refresh_and_global_selection():
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._shutdown_requested = False
    bridge._selection_loading = False
    bridge._job_search, bridge._job_status = "Paris", JobStatus.ANALYZED
    bridge._job_sort_key, bridge._job_sort_ascending = "title", True
    bridge._job_filter_disposition, bridge._job_archive_origin = "", ""
    bridge._job_mission_relation = "direct"
    bridge._minimum_llm_score_percent, bridge._jobs_page = -1, 2
    bridge.service = SimpleNamespace(list_jobs=lambda **kwargs: None)
    calls, emitted = [], []
    bridge._start_read = lambda *args, **kwargs: calls.append(kwargs)
    bridge.allMatchingJobsReady.connect(emitted.append)

    row = SimpleNamespace(
        id=1, status=JobStatus.ANALYZED, application_id=None,
        shortlisted=False, can_generate=True,
    )
    bridge.selectAllMatchingJobs()
    assert calls[-1]["mission_relation"] == "direct"
    bridge._job_mission_relation = "adjacent"
    calls[-1]["on_success"]([row])
    assert emitted == []
    calls[-1]["on_finished"]()
    bridge.selectAllMatchingJobs()
    calls[-1]["on_success"]([row])
    assert emitted[0][0]["id"] == 1

    refreshed = []
    bridge.loadJobs = lambda *args: refreshed.append(args)
    bridge.refreshJobs()
    assert refreshed == [("Paris", JobStatus.ANALYZED, "title", True, "", 2, "", "adjacent")]
