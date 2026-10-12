"""Large history reads stay bounded without weakening admission or monitoring."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from smartapply.database import session_scope
from smartapply.database.analysis_batches import (
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.generation_runs import GenerationItem, GenerationRun
from smartapply.database.models import Job, JobScore, JobStatus
from smartapply.database.session import get_engine
from smartapply.desktop.workspace_views import analysis_workspace_view, generation_workspace_view
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.pipeline.batch_analysis.presentation import analysis_workspace

DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _job(number, *, candidate=False):
    return Job(
        external_id=f"view-{number}", title=f"Engineer {number:04d}", source_company="Example",
        description="large private source " * 1000, source_data={"body": "x" * 10000},
        source="manual", location="Paris", published_date=DATE + timedelta(minutes=number),
        status=JobStatus.SCRAPED if candidate else JobStatus.ANALYZED,
        analyzed_at=None if candidate else DATE,
        filtered_at=DATE,
        score=JobScore(components={"filter_disposition": "pending_relevance",
                                   "hard_reject": HardRejectResult(decisions=()).audit_payload()}),
    )


def _history(kind, count, *, active_oldest=False):
    with session_scope() as session:
        for number in range(count):
            job = _job(number)
            session.add(job)
            session.flush()
            values = dict(id=f"run-{number:04d}", state="active" if number == 0 and active_oldest else "completed",
                          profile_data={"private": "x" * 10000}, created_at=DATE + timedelta(minutes=number))
            if kind == "analysis":
                run = AnalysisBatchRun(**values, provider="openai", model="mock")
            else:
                run = GenerationRun(**values, mode="batch", routes={}, reference_date="2026-01-01")
            session.add(run)
            session.flush()
            values = dict(run_id=run.id, job_id=job.id, job_snapshot="frozen", title=job.title,
                          company="Example", state="extraction_pending" if number == 0 and active_oldest and kind == "analysis"
                          else "waiting" if number == 0 and active_oldest else "completed")
            if kind == "analysis":
                session.add(AnalysisBatchItem(**values, analyzer_input={"body": "x" * 10000}))
                session.add(AnalysisBatchWave(id=f"wave-{number}", run_id=run.id, phase="evaluation",
                                             state="imported", provider_data={"status": "completed", "completed": 1},
                                             requests=[{"body": "x" * 10000}], provider_batch_id=f"batch-{number}"))
            else:
                session.add(GenerationItem(**values, analysis_snapshot="frozen"))


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_history_metadata_and_selected_details_bounded_but_all_pages_accessible(isolated_db, kind):
    _history(kind, 81, active_oldest=True)
    read = analysis_workspace_view if kind == "analysis" else generation_workspace_view
    loaded, statements = [], []
    def record_load(session, obj):
        loaded.append(type(obj).__name__)
    def record_sql(conn, cursor, statement, *args):
        statements.append(statement.lower())
    engine = get_engine()
    from sqlalchemy.orm import Session
    event.listen(Session, "loaded_as_persistent", record_load)
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        result = read(history_page=2, include_candidates=False)
    finally:
        event.remove(Session, "loaded_as_persistent", record_load)
        event.remove(engine, "before_cursor_execute", record_sql)
    assert len(result["runs"]) == 20
    assert result["history_total_count"] == 81 and result["history_total_pages"] == 5
    assert result["selected_run"]["id"] == result["runs"][0]["id"] == "run-0060"
    assert result["selected_run"]["sequence"] == 61
    assert len(result["selected_run"]["items"]) == 1
    assert result["active_run_count"] == result["polling_run_count"] == 1
    assert result["monitor_runs"][0]["id"] == "run-0000"
    assert loaded.count("AnalysisBatchItem") <= 1 and loaded.count("AnalysisBatchWave") <= 1
    assert loaded.count("GenerationItem") <= 1
    for statement in statements:
        if statement.startswith("select"):
            assert "profile_data" not in statement and "request_body" not in statement
            assert ".requests" not in statement and "analyzer_input" not in statement
    last = read(history_page=999, include_candidates=False)
    assert last["history_page"] == 5 and last["runs"][0]["id"] == "run-0000"


@pytest.mark.parametrize("kind", ["analysis", "generation"])
def test_monitor_terminal_outside_page_and_explicit_selection(isolated_db, kind):
    _history(kind, 25, active_oldest=True)
    run_cls = AnalysisBatchRun if kind == "analysis" else GenerationRun
    read = analysis_workspace_view if kind == "analysis" else generation_workspace_view
    with session_scope() as session:
        session.get(run_cls, "run-0000").state = "completed"
    result = read(history_page=1, selected_run_id="run-0001", monitor_run_ids=["run-0000"], include_candidates=False)
    assert result["selected_run"]["id"] == "run-0001"
    assert len(result["runs"]) == 20 and result["history_total_count"] == 25
    assert result["monitor_runs"][0]["id"] == "run-0000"
    assert result["monitor_runs"][0]["status"] == "completed"
    assert result["active_run_count"] == result["polling_run_count"] == 0


def test_candidates_filter_sort_and_global_selection_before_pagination(isolated_db):
    with session_scope() as session:
        session.add_all([_job(number, candidate=True) for number in range(260)])
    first = analysis_workspace_view(candidate_sort="title", candidate_ascending=True)
    second = analysis_workspace_view(candidate_sort="title", candidate_ascending=True, candidate_page=2)
    assert first["candidate_count"] == first["candidates_count"] == 260
    assert len(first["candidates"]) == len(second["candidates"]) == 100
    assert first["matching_candidate_ids"] == second["matching_candidate_ids"]
    assert first["candidates"][0]["title"] == "Engineer 0000"
    assert second["candidates"][0]["title"] == "Engineer 0100"
    filtered = analysis_workspace_view(candidate_query="02", candidate_sort="title", candidate_ascending=False)
    expected = sorted((row for row in analysis_workspace()["candidates"] if "02" in " ".join(str(row[key]) for key in ("title", "company", "location", "id", "state_label"))),
                      key=lambda row: row["title"], reverse=True)
    assert filtered["matching_candidate_ids"] == [row["id"] for row in expected]
    assert filtered["candidates"] == expected
    assert filtered["candidate_count"] == len(expected)
    assert len(filtered["candidate_ids"]) == 260


def test_scoped_analysis_matches_legacy_details_and_does_not_lazy_fetch_batch_ids(isolated_db):
    _history("analysis", 3)
    legacy = analysis_workspace()
    statements = []
    engine = get_engine()
    def record(conn, cursor, statement, *args):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", record)
    try:
        scoped = analysis_workspace(run_ids=["run-0001"], include_candidates=False)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert scoped["runs"] == [next(run for run in legacy["runs"] if run["id"] == "run-0001")]
    assert scoped["candidates"] == []
    wave_reads = [statement for statement in statements if "from analysis_batch_waves" in statement]
    assert len(wave_reads) == 1 and "provider_batch_id" in wave_reads[0]


def test_cancelled_analysis_keeps_sequence_numbers_and_all_errors(isolated_db):
    _history("analysis", 22)
    with session_scope() as session:
        session.get(AnalysisBatchRun, "run-0021").state = "cancelled"
        item = session.query(AnalysisBatchItem).filter_by(run_id="run-0020").one()
        item.state, item.error = "evaluation_failed", "Preserved error"
    result = analysis_workspace_view(selected_run_id="run-0020", include_candidates=False)
    assert result["history_total_count"] == 21
    assert result["selected_run"]["sequence"] == 21
    assert result["selected_run"]["errors"][0]["message"] == "Preserved error"
    assert "run-0021" not in [run["id"] for run in result["runs"]]
