"""A score-policy change must be reversible without losing the candidate dossier."""

import copy
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy import inspect, select, text

from smartapply.cli import cli
from smartapply.database import get_engine, session_scope
from smartapply.database.fit_history import restore_fit
from smartapply.database.fit_schema import ensure_fit_schema
from smartapply.database.models import Job, JobFitHistory
from smartapply.llm.fit_context import LEGACY_POLICY, fingerprint
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from tests.fit_fixtures import global_response
from tests.test_evidence_fit import explicit_fit_routes as _explicit_fit_routes
from tests.test_evidence_fit import seeded_analysis, snapshot

explicit_fit_routes = _explicit_fit_routes


def seed_three_axis():
    job_id = seeded_analysis()
    with session_scope() as session:
        analysis = session.get(Job, job_id).analysis
        diagnostics = copy.deepcopy(analysis.fit_diagnostics)
        diagnostics.update(
            {
                "policy_name": LEGACY_POLICY["name"],
                "policy_fingerprint": fingerprint(LEGACY_POLICY),
                "used_decisions": {
                    "missions": "strong_fit",
                    "essentials": "credible_fit",
                    "responsibilities": "credible_fit",
                },
                "used_levels": {"missions": 4, "essentials": 3, "responsibilities": 3},
                "evaluation": {
                    "decision": {
                        "missions": "strong_fit",
                        "essentials": "credible_fit",
                        "responsibilities": "credible_fit",
                    }
                },
            }
        )
        # Reassign JSON so SQLAlchemy persists the old trace.
        analysis.fit_diagnostics = diagnostics
        analysis.fit_score = 0.84
        analysis.raw_response = {**analysis.raw_response, "fit_score": 0.84}
    return job_id


def test_three_axis_global_three_axis_round_trip_is_lossless(isolated_db):
    job_id = seed_three_axis()
    before = snapshot(job_id)
    result = execute_rescore_plan(
        prepare_rescore_plan(job_ids=[job_id]),
        SimpleNamespace(name="openai", complete_json=lambda **kw: global_response(score=0.6137)),
        approved=True,
    )
    assert result[0]["status"] == "rescored"
    after = snapshot(job_id)
    for key in ("job", "score", "application", "documents"):
        assert after[key] == before[key]
    with session_scope() as session:
        saved = session.scalars(select(JobFitHistory).where(JobFitHistory.job_id == job_id)).one()
        assert saved.snapshot["fit_diagnostics"] == before["analysis"]["fit_diagnostics"]
        assert saved.snapshot["raw_response"] == before["analysis"]["raw_response"]
        history_id = saved.id
        restore_fit(session, job_id, history_id)
    assert snapshot(job_id) == before
    with session_scope() as session:
        histories = session.scalars(select(JobFitHistory).order_by(JobFitHistory.id)).all()
        assert len(histories) == 2
        assert histories[1].snapshot["fit_score"] == 0.6137
        assert histories[1].snapshot["fit_diagnostics"]["score_source"] == "llm"
        assert session.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    from smartapply.desktop.fit_presentation import fit_detail

    assert [
        axis["level"]
        for axis in fit_detail(snapshot(job_id)["analysis"]["fit_diagnostics"])["fit_axes"]
    ] == [4, 3, 3]
    listed = CliRunner().invoke(cli, ["fit-history", "--job-id", str(job_id)])
    assert listed.exit_code == 0
    assert [item["id"] for item in json.loads(listed.output)] == [histories[1].id, history_id]
    restored = CliRunner().invoke(
        cli, ["fit-history", "--job-id", str(job_id), "--restore", str(histories[1].id)]
    )
    assert restored.exit_code == 0
    assert snapshot(job_id)["analysis"]["fit_score"] == 0.6137
    assert fit_detail(snapshot(job_id)["analysis"]["fit_diagnostics"])["fit_axes"] == ()


def test_archive_and_replacement_roll_back_together(isolated_db):
    from smartapply.database.repository.analyses import set_analysis

    job_id = seed_three_axis()
    before = snapshot(job_id)
    with pytest.raises(RuntimeError), session_scope() as session:
        set_analysis(session, job_id, fit_score=0.9, fit_diagnostics={"score_source": "llm"})
        session.flush()
        raise RuntimeError("Publication interrupted")
    assert snapshot(job_id) == before
    with session_scope() as session:
        assert session.scalars(select(JobFitHistory)).all() == []


def test_restore_rejects_another_job_or_corrupted_snapshot(isolated_db):
    from smartapply.database.fit_history import archive_fit

    job_id = seed_three_axis()
    with session_scope() as session:
        saved = archive_fit(session, session.get(Job, job_id).analysis)
        session.flush()
        with pytest.raises(ValueError, match="autre offre"):
            restore_fit(session, job_id + 1, saved.id)
        saved.snapshot = {**saved.snapshot, "fit_score": 0.999}
        with pytest.raises(ValueError, match="intégrité"):
            restore_fit(session, job_id, saved.id)


def test_migration_only_adds_history_and_preserves_original_tables(isolated_db):
    job_id = seed_three_axis()
    before = snapshot(job_id)
    engine = get_engine()
    old_columns = {
        table: [{**col, "type": str(col["type"])} for col in inspect(engine).get_columns(table)]
        for table in ("job_analyses", "applications", "jobs")
    }
    JobFitHistory.__table__.drop(engine)
    assert ensure_fit_schema() == ["CREATE TABLE job_fit_history"]
    assert ensure_fit_schema() == []
    assert snapshot(job_id) == before
    assert {
        table: [{**col, "type": str(col["type"])} for col in inspect(engine).get_columns(table)]
        for table in old_columns
    } == old_columns
