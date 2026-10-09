"""Safety tests for the historical deterministic-rejection backfill."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.maintenance.hard_reject_backfill import (
    BackfillError,
    BackfillPreconditionError,
    apply_backfill,
    plan_backfill,
)


class _KeepUnlessBlockedRejector:
    def evaluate(self, job) -> HardRejectResult:
        if "blocked" in job.title.lower():
            from smartapply.filtering.hard_reject.types import Evidence, GateDecision, GateStatus

            return HardRejectResult(
                decisions=(
                    GateDecision(
                        gate="role",
                        status=GateStatus.BLOCKED,
                        reason_codes=("test_blocker",),
                        evidence=(
                            Evidence(
                                rule_id="test",
                                source="title",
                                quote=job.title,
                            ),
                        ),
                    ),
                )
            )
        return HardRejectResult(decisions=())


def _rejected_job(
    *,
    external_id: str,
    title: str,
    stage: str = "local_filter",
    duplicate_status: str = "none",
):
    from smartapply.database import session_scope
    from smartapply.database.models import JobStatus
    from smartapply.database.repository import mark_archived, set_score, upsert_job

    with session_scope() as session:
        job = upsert_job(
            session,
            external_id=external_id,
            title=title,
            company="Acme",
            description="Python, machine learning, two years of experience.",
            location="Paris",
            contract_type="CDI",
            source="manual",
            status=JobStatus.SCRAPED,
            duplicate_review_status=duplicate_status,
        )
        set_score(
            session,
            job.id,
            components={
                "filter_disposition": "rejected",
                "rejection_stage": stage,
                "rejection_reasons": ["legacy_reason"],
                "rejection_summary": "legacy_reason",
                "semantic": 0.2,
                "independent_audit": {"source": "fixture"},
            },
        )
        mark_archived(session, job.id)
        return int(job.id)


def _database_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def test_backfill_updates_only_plain_local_filter_rejects(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Application, Job, JobAnalysis, JobScore, JobStatus

    safe_id = _rejected_job(external_id="safe", title="Data Scientist")
    blocked_id = _rejected_job(external_id="blocked", title="Blocked role")
    validation_id = _rejected_job(
        external_id="validation", title="ML Engineer", stage="role_validation"
    )
    application_id = _rejected_job(external_id="application", title="ML Engineer")
    alias_id = _rejected_job(
        external_id="alias", title="ML Engineer", duplicate_status="confirmed"
    )
    pending_id = _rejected_job(
        external_id="pending", title="ML Engineer", duplicate_status="pending"
    )
    confirmed_root_id = _rejected_job(
        external_id="confirmed-root", title="ML Engineer", duplicate_status="confirmed"
    )
    analyzed_id = _rejected_job(external_id="analyzed", title="ML Engineer")

    with session_scope() as session:
        alias = session.get(Job, alias_id)
        alias.canonical_job_id = confirmed_root_id
        pending = session.get(Job, pending_id)
        pending.possible_duplicate_of_id = confirmed_root_id
        session.add(Application(job_id=application_id, status=JobStatus.ARCHIVED))
        session.add(
            JobAnalysis(
                job_id=analyzed_id,
                role_type="ML",
                created_at=datetime.now(timezone.utc),
            )
        )

    database = _database_path(isolated_db)
    rejector = _KeepUnlessBlockedRejector()
    plan = plan_backfill(database, rejector)

    assert plan.eligible_rows == 2
    assert plan.change_ids == [safe_id]
    assert plan.still_rejected_rows == 1
    assert plan.exclusion_counts == {
        "confirmed_duplicate_alias": 1,
        "confirmed_duplicate_group": 1,
        "has_application": 1,
        "has_prior_analysis": 1,
        "non_local_filter_decision": 1,
        "pending_duplicate_review": 1,
    }
    with session_scope() as session:
        assert session.get(Job, safe_id).status == JobStatus.ARCHIVED

    report = apply_backfill(plan, rejector, backup_dir=isolated_db / "backups")
    assert report.changed_ids == (safe_id,)
    assert report.metadata_only_verified is True
    assert report.table_counts_unchanged is True
    assert report.backup.stat().st_mode & 0o777 == 0o600

    with sqlite3.connect(report.backup) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        archived = backup.execute(
            "SELECT status FROM jobs WHERE id = ?", (safe_id,)
        ).fetchone()[0]
        assert archived == JobStatus.ARCHIVED

    with session_scope() as session:
        safe = session.get(Job, safe_id)
        assert safe.status == JobStatus.FILTERED
        assert safe.archived_at is None
        assert safe.filtered_at is not None
        score = session.query(JobScore).filter_by(job_id=safe_id).one()
        assert score.components["filter_disposition"] == "pending_relevance"
        assert score.components["hard_reject"]["rejected"] is False
        assert "semantic" not in score.components
        assert score.components["independent_audit"] == {"source": "fixture"}
        assert score.components["hard_reject_backfill"]["previous_rejection"] == {
            "stage": "local_filter",
            "reasons": ["legacy_reason"],
            "summary": "legacy_reason",
        }
        for excluded_id in (
            blocked_id,
            validation_id,
            application_id,
            alias_id,
            pending_id,
            confirmed_root_id,
            analyzed_id,
        ):
            assert session.get(Job, excluded_id).status == JobStatus.ARCHIVED


def test_backfill_refuses_row_drift_before_backup(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus

    job_id = _rejected_job(external_id="drift", title="Data Scientist")
    database = _database_path(isolated_db)
    rejector = _KeepUnlessBlockedRejector()
    plan = plan_backfill(database, rejector)

    with session_scope() as session:
        session.get(Job, job_id).title = "Data Scientist changed after dry run"

    with pytest.raises(BackfillPreconditionError, match="changed after the dry-run"):
        apply_backfill(plan, rejector, backup_dir=isolated_db / "backups")

    assert not list((isolated_db / "backups").glob("*.db"))
    with session_scope() as session:
        assert session.get(Job, job_id).status == JobStatus.ARCHIVED


def test_backfill_refuses_target_trigger_before_any_write(isolated_db) -> None:
    job_id = _rejected_job(external_id="trigger", title="Data Scientist")
    database = _database_path(isolated_db)
    rejector = _KeepUnlessBlockedRejector()
    plan = plan_backfill(database, rejector)

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE trigger_sentinel (value TEXT NOT NULL);
            INSERT INTO trigger_sentinel (value) VALUES ('ORIGINAL');
            CREATE TRIGGER mutate_outside_backfill
            AFTER UPDATE ON jobs
            BEGIN
                UPDATE trigger_sentinel SET value = 'MUTATED';
            END;
            """
        )

    with pytest.raises(BackfillError, match="refuses triggers on target tables"):
        apply_backfill(plan, rejector, backup_dir=isolated_db / "backups")

    assert not list((isolated_db / "backups").glob("*.db"))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM trigger_sentinel").fetchone()[0] == (
            "ORIGINAL"
        )
        assert connection.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0] == "archived"


def test_backfill_rolls_back_every_row_if_an_update_fails(
    isolated_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobScore, JobStatus
    from smartapply.maintenance import hard_reject_backfill

    first_id = _rejected_job(external_id="rollback-1", title="Data Scientist")
    second_id = _rejected_job(external_id="rollback-2", title="ML Engineer")
    database = _database_path(isolated_db)
    rejector = _KeepUnlessBlockedRejector()
    plan = plan_backfill(database, rejector)
    original_update = hard_reject_backfill._update_row
    calls = 0

    def fail_on_second_update(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated update failure")
        original_update(*args, **kwargs)

    monkeypatch.setattr(hard_reject_backfill, "_update_row", fail_on_second_update)

    backup_dir = isolated_db / "backups"
    with pytest.raises(RuntimeError, match="simulated update failure"):
        apply_backfill(plan, rejector, backup_dir=backup_dir)

    backups = list(backup_dir.glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with session_scope() as session:
        for job_id in (first_id, second_id):
            job = session.get(Job, job_id)
            score = session.query(JobScore).filter_by(job_id=job_id).one()
            assert job.status == JobStatus.ARCHIVED
            assert job.archived_at is not None
            assert score.components["filter_disposition"] == "rejected"
