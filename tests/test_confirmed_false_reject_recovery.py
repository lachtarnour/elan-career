"""Safety tests for recovery authorized by the exhaustive replay manifest."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartapply.filtering.hard_reject.input_fingerprint import hard_reject_input_sha256
from smartapply.filtering.hard_reject.types import (
    Evidence,
    GateDecision,
    GateStatus,
    HardRejectResult,
)
from smartapply.maintenance.confirmed_false_reject_recovery import (
    ConfirmedFalseRejectPreconditionError,
    ConfirmedFalseRejectRecoveryError,
    ReplayManifestRequirements,
    apply_confirmed_false_recovery,
    load_replay_manifest,
    plan_confirmed_false_recovery,
)


class _MutableRejector:
    def __init__(self) -> None:
        self.blocked = False

    def evaluate(self, job) -> HardRejectResult:
        if not self.blocked:
            return HardRejectResult(
                decisions=(),
                context_reasons=(f"audit_revalidated:{job.id}",),
            )
        return HardRejectResult(
            decisions=(
                GateDecision(
                    gate="role",
                    status=GateStatus.BLOCKED,
                    reason_codes=("new_blocker",),
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


def _archived_rejection(
    *,
    external_id: str,
    title: str,
    stage: str,
    reason: str,
    duplicate_status: str = "none",
) -> int:
    from smartapply.database import session_scope
    from smartapply.database.models import JobStatus
    from smartapply.database.repository import mark_archived, set_score, upsert_job

    with session_scope() as session:
        job = upsert_job(
            session,
            external_id=external_id,
            title=title,
            company="Acme",
            description="Python machine learning role with two years of experience.",
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
                "rule_based": 0.0,
                "semantic": 0.71,
                "final": 0.75,
                "reasons": [reason],
                "filter_disposition": "rejected",
                "rejection_stage": stage,
                "rejection_reasons": [reason],
                "rejection_summary": reason,
            },
        )
        mark_archived(session, job.id)
        return int(job.id)


def _manifest(path: Path, rows: list[dict]) -> Path:
    diff_records = []
    for row in rows:
        diff_records.append(
            {
                "id": row["id"],
                "title": row["title"],
                "company": "Acme",
                "audited_input_sha256": hard_reject_input_sha256(
                    SimpleNamespace(
                        title=row["title"],
                        company="Acme",
                        description=(
                            "Python machine learning role with two years of experience."
                        ),
                        location="Paris",
                        contract_type="CDI",
                        remote_policy=None,
                        application_url=None,
                        source="manual",
                        source_data=None,
                    )
                ),
                "audit_file": row.get("audit_file", "exhaustive-role.json"),
                "audit_decision": "certain_false_rejection",
                "classification": "confirmed_false_rejection_rescued",
                "classification_reason": "Confirmed by exhaustive audit.",
                "old_disposition": "rejected",
                "old_bucket": row.get("old_bucket", "role_scope"),
                "old_stage": row["stage"],
                "old_reason": row["reason"],
                "old_reasons": [row["reason"]],
                "new_disposition": "pending_relevance",
                "new_rejected": False,
            }
        )
    payload = {
        "ruleset_version": "hard-reject-v2",
        "true_regression_count": 0,
        "audited_ids": len(diff_records),
        "database_verified_ids": len(diff_records),
        "persisted_rejected_ids": len(diff_records),
        "semantic_gate_ids": len(diff_records),
        "semantic_evaluated_ids": len(diff_records),
        "duplicate_identity_only_ids": 0,
        "protected_rejections": 0,
        "certain_false_rejections": len(diff_records),
        "probable_false_rejections": 0,
        "ambiguous_semantic": 0,
        "certain_rescued": len(diff_records),
        "certain_still_rejected_ids": [],
        "diff_record_count": len(diff_records),
        "diff_records": diff_records,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixture_requirements(path: Path) -> ReplayManifestRequirements:
    return ReplayManifestRequirements.for_fixture_payload(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _audit_row(job_id: int, title: str, stage: str, reason: str) -> dict:
    return {"id": job_id, "title": title, "stage": stage, "reason": reason}


def test_recovery_keeps_alias_archived_and_preserves_existing_work(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Application, Job, JobAnalysis, JobScore, JobStatus

    root_id = _archived_rejection(
        external_id="root",
        title="AI Engineer root",
        stage="local_filter",
        reason="legacy_contract_false_positive",
        duplicate_status="confirmed",
    )
    alias_id = _archived_rejection(
        external_id="alias",
        title="AI Engineer alias",
        stage="local_filter",
        reason="legacy_contract_false_positive",
        duplicate_status="confirmed",
    )
    filtered_id = _archived_rejection(
        external_id="filtered",
        title="Filtered AI Engineer",
        stage="local_filter",
        reason="legacy_role_false_positive",
    )
    analyzed_id = _archived_rejection(
        external_id="analyzed",
        title="Analyzed Data Scientist",
        stage="role_validation",
        reason="legacy_role_false_positive",
    )
    ranked_id = _archived_rejection(
        external_id="ranked",
        title="Ranked ML Engineer",
        stage="contract_validation",
        reason="legacy_contract_review_false_positive",
    )
    application_id = _archived_rejection(
        external_id="application",
        title="Applied ML Engineer",
        stage="experience_validation",
        reason="legacy_experience_false_positive",
    )

    filtered_at = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    ranked_at = datetime(2026, 9, 20, 11, tzinfo=timezone.utc)
    analyzed_at = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    with session_scope() as session:
        session.get(Job, alias_id).canonical_job_id = root_id

        filtered = session.get(Job, filtered_id)
        filtered.filtered_at = filtered_at

        analyzed = session.get(Job, analyzed_id)
        analyzed.filtered_at = filtered_at
        analyzed.ranked_at = ranked_at
        analyzed.analyzed_at = analyzed_at
        session.add(
            JobAnalysis(
                job_id=analyzed_id,
                role_type="Data Scientist",
                fit_score=0.88,
                raw_response={"kept": True},
            )
        )

        ranked = session.get(Job, ranked_id)
        ranked.filtered_at = filtered_at
        ranked.ranked_at = ranked_at

        session.add(Application(job_id=application_id, status=JobStatus.ARCHIVED))

    rows = [
        _audit_row(root_id, "AI Engineer root", "local_filter", "legacy_contract_false_positive"),
        _audit_row(alias_id, "AI Engineer alias", "local_filter", "legacy_contract_false_positive"),
        _audit_row(
            filtered_id,
            "Filtered AI Engineer",
            "local_filter",
            "legacy_role_false_positive",
        ),
        _audit_row(
            analyzed_id,
            "Analyzed Data Scientist",
            "role_validation",
            "legacy_role_false_positive",
        ),
        _audit_row(
            ranked_id,
            "Ranked ML Engineer",
            "contract_validation",
            "legacy_contract_review_false_positive",
        ),
        _audit_row(
            application_id,
            "Applied ML Engineer",
            "experience_validation",
            "legacy_experience_false_positive",
        ),
    ]
    manifest = _manifest(isolated_db / "replay.json", rows)
    database = isolated_db / "test.db"
    rejector = _MutableRejector()
    plan = plan_confirmed_false_recovery(
        database,
        manifest,
        rejector,
        manifest_requirements=_fixture_requirements(manifest),
    )

    assert plan.change_ids == [root_id, filtered_id, analyzed_id, ranked_id]
    assert plan.excluded_ids == {
        "confirmed_duplicate_alias": (alias_id,),
        "has_application": (application_id,),
    }
    assert [change.history_state for change in plan.changes] == [
        "fresh",
        "filtered",
        "analyzed",
        "ranked",
    ]
    assert plan.to_dict()["history_states"]["filtered"] == [filtered_id]

    with session_scope() as session:
        filtered_at_snapshot = session.get(Job, filtered_id).filtered_at
        analyzed_score = session.query(JobScore).filter_by(job_id=analyzed_id).one()
        ranked_score = session.query(JobScore).filter_by(job_id=ranked_id).one()
        analyzed_snapshot = {
            "timestamps": (
                session.get(Job, analyzed_id).filtered_at,
                session.get(Job, analyzed_id).ranked_at,
                session.get(Job, analyzed_id).analyzed_at,
            ),
            "computed_at": analyzed_score.computed_at,
        }
        ranked_snapshot = {
            "timestamps": (
                session.get(Job, ranked_id).filtered_at,
                session.get(Job, ranked_id).ranked_at,
                session.get(Job, ranked_id).analyzed_at,
            ),
            "computed_at": ranked_score.computed_at,
        }

    report = apply_confirmed_false_recovery(
        plan,
        rejector,
        backup_dir=isolated_db / "backups",
    )
    assert report.changed_ids == (root_id, filtered_id, analyzed_id, ranked_id)
    assert report.fresh_ids == (root_id,)
    assert report.history_preserved_ids == (filtered_id, analyzed_id, ranked_id)
    assert report.metadata_only_verified is True
    assert report.table_counts_unchanged is True
    assert report.backup.stat().st_mode & 0o777 == 0o600

    with sqlite3.connect(report.backup) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT status FROM jobs WHERE id = ?", (root_id,)
        ).fetchone()[0] == JobStatus.ARCHIVED

    with session_scope() as session:
        root = session.get(Job, root_id)
        root_score = session.query(JobScore).filter_by(job_id=root_id).one()
        assert root.status == JobStatus.FILTERED
        assert root.archived_at is None
        assert root.filtered_at is not None
        assert "semantic" not in root_score.components
        assert "final" not in root_score.components

        filtered = session.get(Job, filtered_id)
        filtered_score = session.query(JobScore).filter_by(job_id=filtered_id).one()
        assert filtered.status == JobStatus.FILTERED
        assert filtered.archived_at is None
        assert filtered.filtered_at == filtered_at_snapshot
        assert filtered.ranked_at is None
        assert "semantic" not in filtered_score.components
        assert "final" not in filtered_score.components

        analyzed = session.get(Job, analyzed_id)
        analyzed_score = session.query(JobScore).filter_by(job_id=analyzed_id).one()
        assert analyzed.status == JobStatus.ANALYZED
        assert analyzed.archived_at is None
        assert (analyzed.filtered_at, analyzed.ranked_at, analyzed.analyzed_at) == (
            analyzed_snapshot["timestamps"]
        )
        assert analyzed_score.computed_at == analyzed_snapshot["computed_at"]
        assert "semantic" not in analyzed_score.components
        assert "final" not in analyzed_score.components

        ranked = session.get(Job, ranked_id)
        ranked_score = session.query(JobScore).filter_by(job_id=ranked_id).one()
        assert ranked.status == JobStatus.FILTERED
        assert ranked.archived_at is None
        assert (ranked.filtered_at, ranked.ranked_at, ranked.analyzed_at) == (
            ranked_snapshot["timestamps"]
        )
        assert ranked_score.computed_at == ranked_snapshot["computed_at"]
        assert "semantic" not in ranked_score.components
        assert "final" not in ranked_score.components

        for job_id in (root_id, filtered_id, analyzed_id, ranked_id):
            components = session.query(JobScore).filter_by(job_id=job_id).one().components
            assert components["filter_disposition"] == "pending_relevance"
            assert components["hard_reject"]["ruleset_version"] == "hard-reject-v2"
            assert components["hard_reject"]["rejected"] is False
            assert components["confirmed_false_rejection_recovery"]["manifest_sha256"] == (
                report.manifest_sha256
            )
            assert components["confirmed_false_rejection_recovery"]["previous_rejection"][
                "filter_disposition"
            ] == "rejected"
            assert "rejection_stage" not in components
            assert "rejection_reasons" not in components
            assert "rejection_summary" not in components

        assert session.get(Job, alias_id).status == JobStatus.ARCHIVED
        assert session.get(Job, application_id).status == JobStatus.ARCHIVED
        assert session.query(JobAnalysis).filter_by(job_id=analyzed_id).count() == 1


def test_manifest_must_be_a_clean_hard_reject_v2_replay(isolated_db) -> None:
    job_id = _archived_rejection(
        external_id="manifest-guard",
        title="Data Scientist",
        stage="local_filter",
        reason="legacy_false_positive",
    )
    manifest = _manifest(
        isolated_db / "replay.json",
        [_audit_row(job_id, "Data Scientist", "local_filter", "legacy_false_positive")],
    )
    payload = json.loads(manifest.read_text())
    payload["true_regression_count"] = 1
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ConfirmedFalseRejectRecoveryError,
        match="unresolved true regressions",
    ):
        load_replay_manifest(manifest)


def test_default_manifest_anchor_rejects_self_consistent_arbitrary_json(isolated_db) -> None:
    job_id = _archived_rejection(
        external_id="untrusted-manifest",
        title="Data Scientist",
        stage="local_filter",
        reason="legacy_false_positive",
    )
    manifest = _manifest(
        isolated_db / "untrusted-replay.json",
        [_audit_row(job_id, "Data Scientist", "local_filter", "legacy_false_positive")],
    )

    with pytest.raises(
        ConfirmedFalseRejectRecoveryError,
        match="does not match the trusted classification-audit-20261005 audit anchor",
    ):
        load_replay_manifest(manifest)

    fixture = load_replay_manifest(
        manifest,
        requirements=_fixture_requirements(manifest),
    )
    assert [entry.job_id for entry in fixture.entries] == [job_id]


def test_manifest_anchor_detects_recovery_evidence_drift(isolated_db) -> None:
    job_id = _archived_rejection(
        external_id="evidence-drift",
        title="Data Scientist",
        stage="local_filter",
        reason="legacy_false_positive",
    )
    manifest = _manifest(
        isolated_db / "evidence-replay.json",
        [_audit_row(job_id, "Data Scientist", "local_filter", "legacy_false_positive")],
    )
    requirements = _fixture_requirements(manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["diff_records"][0]["old_reason"] = "rewritten_false_positive"
    payload["diff_records"][0]["old_reasons"] = ["rewritten_false_positive"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ConfirmedFalseRejectRecoveryError,
        match="recovery evidence does not match the trusted isolated-test-fixture",
    ):
        load_replay_manifest(manifest, requirements=requirements)


def test_manifest_anchor_detects_classification_reason_drift(isolated_db) -> None:
    job_id = _archived_rejection(
        external_id="classification-reason-drift",
        title="Data Scientist",
        stage="local_filter",
        reason="legacy_false_positive",
    )
    manifest = _manifest(
        isolated_db / "classification-reason-replay.json",
        [_audit_row(job_id, "Data Scientist", "local_filter", "legacy_false_positive")],
    )
    requirements = _fixture_requirements(manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["diff_records"][0]["classification_reason"] = "Rewritten justification."
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ConfirmedFalseRejectRecoveryError,
        match="recovery evidence does not match the trusted isolated-test-fixture",
    ):
        load_replay_manifest(manifest, requirements=requirements)


def test_plan_excludes_job_when_audited_decision_inputs_drift(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job

    job_id = _archived_rejection(
        external_id="input-drift",
        title="Data Scientist",
        stage="local_filter",
        reason="legacy_false_positive",
    )
    manifest = _manifest(
        isolated_db / "input-drift-replay.json",
        [_audit_row(job_id, "Data Scientist", "local_filter", "legacy_false_positive")],
    )
    requirements = _fixture_requirements(manifest)
    with session_scope() as session:
        session.get(Job, job_id).description = "This content changed after the frozen audit."

    plan = plan_confirmed_false_recovery(
        isolated_db / "test.db",
        manifest,
        _MutableRejector(),
        manifest_requirements=requirements,
    )

    assert plan.change_ids == []
    assert plan.excluded_ids == {"audit_input_mismatch": (job_id,)}


def test_apply_revalidates_hard_reject_before_backup(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus

    job_id = _archived_rejection(
        external_id="revalidate",
        title="Data Scientist",
        stage="role_validation",
        reason="legacy_role_false_positive",
    )
    manifest = _manifest(
        isolated_db / "replay.json",
        [
            _audit_row(
                job_id,
                "Data Scientist",
                "role_validation",
                "legacy_role_false_positive",
            )
        ],
    )
    rejector = _MutableRejector()
    plan = plan_confirmed_false_recovery(
        isolated_db / "test.db",
        manifest,
        rejector,
        manifest_requirements=_fixture_requirements(manifest),
    )
    rejector.blocked = True

    with pytest.raises(
        ConfirmedFalseRejectPreconditionError,
        match="no longer passes hard-reject-v2",
    ):
        apply_confirmed_false_recovery(
            plan,
            rejector,
            backup_dir=isolated_db / "backups",
        )

    assert not list((isolated_db / "backups").glob("*.db"))
    with session_scope() as session:
        assert session.get(Job, job_id).status == JobStatus.ARCHIVED


def test_recovery_refuses_target_trigger_before_any_write(isolated_db) -> None:
    job_id = _archived_rejection(
        external_id="trigger",
        title="Data Scientist",
        stage="local_filter",
        reason="legacy_false_positive",
    )
    manifest = _manifest(
        isolated_db / "trigger-replay.json",
        [_audit_row(job_id, "Data Scientist", "local_filter", "legacy_false_positive")],
    )
    database = isolated_db / "test.db"
    rejector = _MutableRejector()
    plan = plan_confirmed_false_recovery(
        database,
        manifest,
        rejector,
        manifest_requirements=_fixture_requirements(manifest),
    )

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE trigger_sentinel (value TEXT NOT NULL);
            INSERT INTO trigger_sentinel (value) VALUES ('ORIGINAL');
            CREATE TRIGGER mutate_outside_recovery
            AFTER UPDATE ON job_scores
            BEGIN
                UPDATE trigger_sentinel SET value = 'MUTATED';
            END;
            """
        )

    with pytest.raises(
        ConfirmedFalseRejectRecoveryError,
        match="refuses triggers on target tables",
    ):
        apply_confirmed_false_recovery(
            plan,
            rejector,
            backup_dir=isolated_db / "backups",
        )

    assert not list((isolated_db / "backups").glob("*.db"))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM trigger_sentinel").fetchone()[0] == (
            "ORIGINAL"
        )
        assert connection.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0] == "archived"


def test_apply_refuses_manifest_byte_drift(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus

    job_id = _archived_rejection(
        external_id="manifest-drift",
        title="ML Engineer",
        stage="role_validation",
        reason="legacy_role_false_positive",
    )
    manifest = _manifest(
        isolated_db / "replay.json",
        [
            _audit_row(
                job_id,
                "ML Engineer",
                "role_validation",
                "legacy_role_false_positive",
            )
        ],
    )
    rejector = _MutableRejector()
    plan = plan_confirmed_false_recovery(
        isolated_db / "test.db",
        manifest,
        rejector,
        manifest_requirements=_fixture_requirements(manifest),
    )
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(
        ConfirmedFalseRejectPreconditionError,
        match="manifest changed after planning",
    ):
        apply_confirmed_false_recovery(
            plan,
            rejector,
            backup_dir=isolated_db / "backups",
        )

    assert not list((isolated_db / "backups").glob("*.db"))
    with session_scope() as session:
        assert session.get(Job, job_id).status == JobStatus.ARCHIVED
