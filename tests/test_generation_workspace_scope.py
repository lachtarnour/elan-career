"""Selected generation details scale with their journal, not the full history."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from smartapply.database import get_engine, write_session_scope
from smartapply.database.generation_runs import (
    GenerationCall,
    GenerationItem,
    GenerationItemContext,
    GenerationRun,
    GenerationWave,
)
from smartapply.database.models import Job
from smartapply.desktop.workspace_views import generation_workspace_view
from smartapply.pipeline.generation.presentation import generation_workspace

pytestmark = pytest.mark.usefixtures("isolated_db")


def _seed(count=4, *, tied=False):
    private = "Private prompt/profile/response never needed by a detail view " * 100
    with write_session_scope() as session:
        for number in range(count):
            run_id = f"run-{number:04d}"
            created = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(
                minutes=0 if tied else number,
            )
            session.add(GenerationRun(
                id=run_id, mode="batch", state="active" if number % 2 else "completed",
                profile_data={"private": private},
                routes={"cv_adaptation": {"provider": "openai", "model": "current-model"}},
                reference_date="2026-10-11", created_at=created, updated_at=created,
            ))
            session.flush()
            wave = GenerationWave(
                id=f"wave-{number}", run_id=run_id, state="submitted", provider="openai",
                provider_batch_id=f"remote-{number}", requests=[{"private": private}],
                consumed_ids=[], provider_data={"status": "in_progress", "total": 2},
            )
            session.add(wave)
            session.flush()
            for offer in range(2):
                job = Job(
                    external_id=f"job-{number}-{offer}", source="manual", title=f"AI Engineer {offer}",
                    source_company="Acme", description="Python ML",
                )
                session.add(job)
                session.flush()
                item = GenerationItem(
                    run_id=run_id, job_id=job.id, title=job.title, company=job.source_company,
                    state="waiting" if number % 2 else "completed", result={"private": private},
                    job_snapshot="saved", analysis_snapshot="saved", error="" if offer else "Diagnostic",
                )
                session.add(item)
                session.flush()
                if offer == 0:
                    session.add(GenerationItemContext(
                        item_id=item.id, profile_data={"private": private}, reference_date="2026-10-10",
                        routes={"cv_adaptation": {"provider": "anthropic", "model": "original-model"}},
                    ))
                for position, purpose in enumerate(("cv_adaptation", "application_texts", "document_review")):
                    session.add(GenerationCall(
                        item_id=item.id, position=position, purpose=purpose,
                        provider="openai", schema_name="schema", request_body={"private": private},
                        request_key="saved", response_data={"private": private}, wave_id=wave.id,
                        state="submitted" if position == 2 and number % 2 else "completed",
                    ))


@pytest.mark.parametrize("tied", [False, True])
def test_scoped_details_match_full_workspace_order_global_sequences_and_original_contexts(tied):
    _seed(tied=tied)
    full = generation_workspace()
    if tied:
        assert [run["id"] for run in full["runs"]] == ["run-0003", "run-0002", "run-0001", "run-0000"]
        assert [run["sequence"] for run in full["runs"]] == [4, 3, 2, 1]
    selected = ("run-0001", "run-0003", "run-0001")
    scoped = generation_workspace(run_ids=selected, include_availability=False)
    assert scoped["runs"] == [run for run in full["runs"] if run["id"] in selected]
    assert scoped["reserved_job_ids"] == scoped["resumable_job_ids"] == []
    assert scoped["active_run_count"] == 2
    assert scoped["active_run_id"] == scoped["runs"][0]["id"]
    for run in scoped["runs"]:
        assert run["provider_label"] == "anthropic · openai"
        assert run["model"] == "original-model · current-model"
        assert len(run["items"]) == 2 and run["phase_batches"][-2]["batches"]
    assert "Private prompt" not in str(scoped)
    desktop = generation_workspace_view(selected_run_id="run-0001")
    detail = desktop["selected_run"]
    metadata = next(run for run in desktop["runs"] if run["id"] == detail["id"])
    assert metadata["created_at"] == detail["created_at"]
    assert metadata["sequence"] == detail["sequence"]
    assert detail["created_at"].endswith("+00:00") and detail["updated_at"].endswith("+00:00")


def test_one_run_reads_only_scoped_small_columns_with_large_history(monkeypatch):
    _seed(120)
    statements = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    def forbidden_availability(*_args, **_kwargs):
        pytest.fail("Detail hydration must not compute global availability")

    monkeypatch.setattr(
        "smartapply.pipeline.generation.offer_resume.resumable_generation_job_ids", forbidden_availability,
    )
    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        scoped = generation_workspace(run_ids=["run-0061"], include_availability=False)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(scoped["runs"]) == 1 and len(scoped["runs"][0]["items"]) == 2
    assert scoped["runs"][0]["sequence"] == 62
    assert len(statements) == 6
    for statement, parameters in statements:
        for column in ("profile_data", "request_body", "response_data", ".requests", ".result"):
            assert column not in statement
        if "generation_runs.routes" in statement:
            assert "run-0061" in parameters and "generation_runs.id IN" in statement
        elif "generation_runs.id" not in statement:
            assert "run-0061" in parameters and " IN " in statement
        if "FROM generation_calls" in statement or "FROM generation_item_contexts" in statement:
            assert "JOIN generation_items" in statement and "generation_items.run_id IN" in statement
    assert scoped["reserved_job_ids"] == scoped["resumable_job_ids"] == []


def test_scoped_availability_receives_only_selected_offer_ids_and_defaults_stay_global(monkeypatch):
    _seed()
    received = []

    def availability(_session, job_ids=None):
        received.append(job_ids)
        return {int(job_id) for job_id in job_ids} if job_ids is not None else {999}

    monkeypatch.setattr("smartapply.pipeline.generation.offer_resume.resumable_generation_job_ids", availability)
    scoped = generation_workspace(run_ids=["run-0003"])
    job_ids = [item["id"] for item in scoped["runs"][0]["items"]]
    assert received == [job_ids]
    assert scoped["resumable_job_ids"] == scoped["reserved_job_ids"] == job_ids
    full = generation_workspace()
    assert received[-1] is None and full["resumable_job_ids"] == [999]
    assert len(full["runs"]) == 4


@pytest.mark.parametrize("run_ids", [[], ["unknown"]])
def test_empty_or_unknown_selection_has_no_details_or_availability(monkeypatch, run_ids):
    _seed()

    def availability(_session, job_ids=None):
        assert job_ids == []
        return set()

    monkeypatch.setattr("smartapply.pipeline.generation.offer_resume.resumable_generation_job_ids", availability)
    assert generation_workspace(run_ids=run_ids) == {
        "runs": [], "reserved_job_ids": [], "resumable_job_ids": [],
        "active_run_count": 0, "active_run_id": "",
    }
