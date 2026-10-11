"""Behavioral and transaction safety for the historical filter migration."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartapply.filtering.hard_reject.types import (
    Evidence,
    GateDecision,
    GateStatus,
    HardRejectResult,
)
from smartapply.maintenance._sqlite_safety import SQLiteSafetyError, UnsafeOutputPathError
from smartapply.maintenance.legacy_filter_migration import (
    LegacyFilterMigrationError,
    LegacyFilterMigrationPreconditionError,
    apply_legacy_filter_migration,
    plan_legacy_filter_migration,
)


class _Rejector:
    def evaluate(self, job) -> HardRejectResult:
        if "blocked" not in job.title.lower():
            return HardRejectResult(decisions=())
        return HardRejectResult(decisions=(GateDecision(
            gate="role", status=GateStatus.BLOCKED, reason_codes=("off_target",),
            evidence=(Evidence(rule_id="fixture", source="title", quote=job.title),),
        ),))


def _legacy_job(
    title: str, *, disposition: str = "uncertain", components: dict | None = None,
) -> int:
    from smartapply.database import session_scope
    from smartapply.database.models import JobStatus
    from smartapply.database.repository import set_score, upsert_job

    with session_scope() as session:
        job = upsert_job(
            session, external_id=title, title=title, company="Fixture",
            description="Python and machine learning.", location="Paris",
            contract_type="CDI", source="manual", status=JobStatus.SCRAPED,
        )
        set_score(
            session, job.id, components={
                "filter_disposition": disposition, "reasons": ["legacy_unclear"],
                "semantic": {"value": 0.6}, "title": 0.8, "final": 0.75,
                "independent_audit": {"verified": True}, **(components or {}),
            },
        )
        return int(job.id)


def _database(isolated_db: Path) -> Path:
    return isolated_db / "test.db"


def _snapshot(database: Path) -> dict[str, list[dict]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return {
            str(table[0]): [dict(row) for row in connection.execute(
                f'SELECT * FROM "{table[0]}" ORDER BY rowid'
            )]
            for table in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }


def _plan(database: Path, *, allowed: set[int] | None = None, **kwargs):
    return plan_legacy_filter_migration(
        database, _Rejector(), allowed_rejected_ids=frozenset(allowed or ()), **kwargs,
    )


def test_migration_preserves_workflow_and_discards_legacy_match(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Application, GeneratedDocument, Job, JobAnalysis

    fresh = _legacy_job("ML Engineer")
    analyzed = _legacy_job("Data Scientist")
    with session_scope() as session:
        job = session.get(Job, analyzed)
        now = datetime.now(timezone.utc)
        job.filtered_at = job.ranked_at = job.analyzed_at = now
        job.shortlisted_at = now
        job.shortlist_origin = "manual"
        job.status = "sent"
        session.add(JobAnalysis(job_id=analyzed, fit_score=8.7, raw_response={"paid": True}))
        app = Application(job_id=analyzed, status="sent", form_submitted_at=now)
        session.add(app)
        session.flush()
        session.add(GeneratedDocument(application_id=app.id, doc_type="cv", content="preserve"))
    database = _database(isolated_db)
    before = _snapshot(database)
    plan = _plan(database)
    assert plan.change_ids == (fresh, analyzed)
    assert _snapshot(database) == before
    report = apply_legacy_filter_migration(plan, _Rejector())
    after = _snapshot(database)
    assert after["jobs"] == before["jobs"]
    for table in set(before) - {"job_scores"}:
        assert after[table] == before[table]
    for old, new in zip(before["job_scores"], after["job_scores"], strict=True):
        assert {k: v for k, v in old.items() if k != "components"} == {
            k: v for k, v in new.items() if k != "components"
        }
        old_components, components = json.loads(old["components"]), json.loads(new["components"])
        assert components["filter_disposition"] == "pending_relevance"
        assert components["hard_reject"]["rejected"] is False
        assert components["legacy_filter_migration"]["previous_filter_components"] == {
            "filter_disposition": "uncertain", "reasons": ["legacy_unclear"],
        }
        for key in ("semantic", "title", "final"):
            assert key not in components
        assert components["independent_audit"] == old_components["independent_audit"]
    assert report.backup.stat().st_mode & 0o777 == 0o600
    assert _snapshot(report.backup) == before
    assert _plan(database).changes == ()


def test_new_blocker_archives_only_untouched_rows(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Application, Job, JobAnalysis

    fresh = _legacy_job("Blocked fresh")
    analyzed = _legacy_job("Blocked analyzed")
    sent = _legacy_job("Blocked sent")
    manual = _legacy_job("Blocked manual", components={"manual_shortlist": {"selected": False}})
    pending = _legacy_job("Blocked pending")
    canonical = _legacy_job("Blocked canonical")
    alias = _legacy_job("Blocked alias")
    archived = _legacy_job("Blocked archived", components={
        "rejection_stage": "manual", "rejection_reasons": ["manual_archive"],
        "rejection_summary": "manual_archive",
    })
    shortlisted = _legacy_job("Blocked shortlist")
    with session_scope() as session:
        now = datetime.now(timezone.utc)
        job = session.get(Job, analyzed)
        job.analyzed_at = job.filtered_at = job.ranked_at = now
        job.status = "analyzed"
        session.add(JobAnalysis(job_id=analyzed, fit_score=8.7))
        session.add(Application(job_id=sent, status="sent", form_submitted_at=now))
        job = session.get(Job, pending)
        job.duplicate_review_status = "pending"
        job.possible_duplicate_of_id = canonical
        job = session.get(Job, alias)
        job.canonical_job_id = canonical
        job.duplicate_review_status = "confirmed"
        job = session.get(Job, canonical)
        job.duplicate_review_status = "confirmed"
        job = session.get(Job, archived)
        job.status = "archived"
        job.archived_at = now
        job = session.get(Job, shortlisted)
        job.shortlisted_at = now
        job.shortlist_origin = "manual"
    ids = {fresh, analyzed, sent, manual, pending, canonical, alias, archived, shortlisted}
    database = _database(isolated_db)
    before = _snapshot(database)
    plan = _plan(database, allowed=ids)
    assert plan.to_dict()["rejected_rows"] == 9
    assert plan.to_dict()["newly_archived_rows"] == 1
    report = apply_legacy_filter_migration(plan, _Rejector())
    assert report.newly_archived_ids == (fresh,)
    after = _snapshot(database)
    for old, new in zip(before["jobs"], after["jobs"], strict=True):
        if old["id"] == fresh:
            assert new["status"] == "archived"
            assert new["archived_at"] == report.applied_at
            for key in set(old) - {"status", "archived_at"}:
                assert new[key] == old[key]
        else:
            assert new == old
    for score in after["job_scores"]:
        components = json.loads(score["components"])
        assert components["filter_disposition"] == "rejected"
        assert components["hard_reject"]["rejected"] is True
        if score["job_id"] == archived:
            assert components["rejection_stage"] == "manual"
            assert components["rejection_reasons"] == ["manual_archive"]
            assert components["rejection_summary"] == "manual_archive"
        else:
            assert components["rejection_stage"] == "local_filter"
            assert components["rejection_reasons"] == ["off_target"]
    assert _plan(database, allowed=ids).changes == ()


def test_surviving_manual_archive_retains_archive_reason(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job

    job_id = _legacy_job("ML manually archived", components={
        "rejection_stage": "manual", "rejection_reasons": ["manual_archive"],
        "rejection_summary": "manual_archive",
    })
    with session_scope() as session:
        job = session.get(Job, job_id)
        job.status = "archived"
        job.archived_at = datetime.now(timezone.utc)
    database = _database(isolated_db)
    before = _snapshot(database)
    apply_legacy_filter_migration(_plan(database), _Rejector())
    after = _snapshot(database)
    assert before["jobs"] == after["jobs"]
    components = json.loads(after["job_scores"][0]["components"])
    assert components["filter_disposition"] == "pending_relevance"
    assert components["rejection_stage"] == "manual"
    assert components["rejection_reasons"] == ["manual_archive"]


def test_unreviewed_blocker_refuses_whole_plan_even_if_protected(isolated_db) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Application

    _legacy_job("ML Engineer")
    blocked = _legacy_job("Blocked sent")
    with session_scope() as session:
        session.add(Application(job_id=blocked, status="sent"))
    database = _database(isolated_db)
    before = _snapshot(database)
    with pytest.raises(LegacyFilterMigrationError, match="outside reviewed IDs"):
        _plan(database)
    assert _snapshot(database) == before


def test_relevant_requires_explicit_scope_and_current_hard_reject_is_excluded(isolated_db) -> None:
    _legacy_job("Current classification", components={"hard_reject": {"rejected": False}})
    relevant = _legacy_job("Legacy relevant", disposition="relevant")
    database = _database(isolated_db)
    plan = _plan(database)
    assert plan.changes == ()
    assert plan.excluded_existing_hard_reject == 1
    explicit = _plan(database, dispositions=("uncertain", "relevant"))
    assert explicit.change_ids == (relevant,)
    apply_legacy_filter_migration(explicit, _Rejector())
    rows = _snapshot(database)["job_scores"]
    assert json.loads(rows[0]["components"])["filter_disposition"] == "uncertain"
    assert json.loads(rows[1]["components"])["filter_disposition"] == "pending_relevance"


def test_row_and_manifest_drift_are_refused_before_backup(isolated_db) -> None:
    job_id = _legacy_job("ML Engineer")
    database = _database(isolated_db)
    manifest = isolated_db / "review.json"
    manifest.write_text("{}", encoding="utf-8")
    plan = _plan(database, review_manifest=manifest)
    manifest.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(LegacyFilterMigrationPreconditionError, match="manifest changed"):
        apply_legacy_filter_migration(plan, _Rejector())
    assert not list((isolated_db / "backups").glob("*.db"))
    plan = _plan(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE jobs SET title='changed' WHERE id=?", (job_id,))
    before = _snapshot(database)
    with pytest.raises(LegacyFilterMigrationPreconditionError, match="changed after planning"):
        apply_legacy_filter_migration(plan, _Rejector())
    assert not list((isolated_db / "backups").glob("*.db"))
    assert _snapshot(database) == before


def test_changed_deterministic_decision_is_refused(isolated_db) -> None:
    job_id = _legacy_job("Blocked role")
    database = _database(isolated_db)
    plan = _plan(database, allowed={job_id})

    class ChangedRejector:
        def evaluate(self, job):
            return HardRejectResult(decisions=())

    with pytest.raises(LegacyFilterMigrationPreconditionError, match="Decision.*changed"):
        apply_legacy_filter_migration(plan, ChangedRejector())
    assert not list((isolated_db / "backups").glob("*.db"))


def test_trigger_refusal_happens_before_backup_or_mutation(isolated_db) -> None:
    _legacy_job("ML Engineer")
    database = _database(isolated_db)
    plan = _plan(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TRIGGER forbidden AFTER UPDATE ON job_scores "
                           "BEGIN UPDATE jobs SET title='damaged'; END")
    before = _snapshot(database)
    with pytest.raises(SQLiteSafetyError, match="refuses triggers"):
        apply_legacy_filter_migration(plan, _Rejector())
    assert _snapshot(database) == before
    assert not list((isolated_db / "backups").glob("*.db"))


def test_partial_failure_rolls_back_all_changes(isolated_db, monkeypatch) -> None:
    from smartapply.maintenance import legacy_filter_migration as migration

    first = _legacy_job("Blocked first")
    second = _legacy_job("Blocked second")
    database = _database(isolated_db)
    plan = _plan(database, allowed={first, second})
    before = _snapshot(database)
    original = migration._update_row

    def fail_second(connection, change, components, *, applied_at):
        original(connection, change, components, applied_at=applied_at)
        if change.job_id == second:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(migration, "_update_row", fail_second)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        apply_legacy_filter_migration(plan, _Rejector())
    assert _snapshot(database) == before
    backups = list((isolated_db / "backups").glob("*.db"))
    assert len(backups) == 1
    assert _snapshot(backups[0]) == before


def test_full_database_diff_catches_unauthorized_related_table_change(isolated_db, monkeypatch) -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import JobAnalysis
    from smartapply.maintenance import legacy_filter_migration as migration

    job_id = _legacy_job("ML Engineer")
    with session_scope() as session:
        session.add(JobAnalysis(job_id=job_id, fit_score=8.7))
    database = _database(isolated_db)
    plan = _plan(database)
    before = _snapshot(database)
    original = migration._update_row

    def corrupt_analysis(connection, change, components, *, applied_at):
        original(connection, change, components, applied_at=applied_at)
        connection.execute("UPDATE job_analyses SET fit_score=0 WHERE job_id=?", (job_id,))

    monkeypatch.setattr(migration, "_update_row", corrupt_analysis)
    with pytest.raises(LegacyFilterMigrationError, match="unauthorized database value"):
        apply_legacy_filter_migration(plan, _Rejector())
    assert _snapshot(database) == before


@pytest.mark.parametrize("collision", [
    "database", "wal", "manifest", "profile", "hardlink", "expected_plan",
])
def test_cli_refuses_output_collisions_before_loading_inputs(
    isolated_db, monkeypatch, collision,
) -> None:
    from scripts import migrate_legacy_filter as command

    database = _database(isolated_db)
    manifest = isolated_db / "review.json"
    manifest.write_text("preserve", encoding="utf-8")
    expected_plan = isolated_db / "expected-plan.json"
    expected_plan.write_text("preserve", encoding="utf-8")
    profile_dir = isolated_db / "profile"
    profile_dir.mkdir()
    profile = profile_dir / "profile.yaml"
    profile.write_text("preserve", encoding="utf-8")
    output = {
        "database": database, "wal": Path(f"{database}-wal"),
        "manifest": manifest, "profile": profile,
        "hardlink": isolated_db / "manifest-link.json",
        "expected_plan": expected_plan,
    }[collision]
    if collision == "hardlink":
        output.hardlink_to(manifest)
    monkeypatch.setattr(sys, "argv", [
        "migrate", "--database", str(database), "--review-manifest", str(manifest),
        "--profile-dir", str(profile_dir), "--output", str(output), "--apply",
        "--expected-plan", str(expected_plan),
    ])
    with pytest.raises(UnsafeOutputPathError, match="protected input"):
        command.main()
    assert manifest.read_text(encoding="utf-8") == "preserve"
    assert profile.read_text(encoding="utf-8") == "preserve"
    assert not list((isolated_db / "backups").glob("*.db"))


def test_cli_default_is_read_only_and_emits_compact_summary(isolated_db, monkeypatch, capsys) -> None:
    from scripts import migrate_legacy_filter as command

    _legacy_job("ML Engineer")
    database = _database(isolated_db)
    manifest = isolated_db / "review.json"
    manifest.write_text("{}", encoding="utf-8")
    profile_dir = isolated_db / "profile"
    profile_dir.mkdir()
    output = isolated_db / "report.json"
    before = _snapshot(database)
    monkeypatch.setattr(command, "load_manifest", lambda path: SimpleNamespace(
        allowed_uncertain_rejected_ids=frozenset(), allowed_rejected_ids=frozenset(),
    ))
    monkeypatch.setattr(command, "build_rejector", lambda profile: _Rejector())
    monkeypatch.setattr(command, "load_profile", lambda path: None)
    monkeypatch.setattr(sys, "argv", [
        "migrate", "--database", str(database), "--review-manifest", str(manifest),
        "--profile-dir", str(profile_dir), "--output", str(output),
    ])
    command.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["planned_rows"] == 1
    assert payload["pending_relevance_rows"] == 1
    assert "changes" not in payload
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert _snapshot(database) == before


def test_cli_module_entry_point_is_executable() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.migrate_legacy_filter", "--help"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--review-manifest" in result.stdout
    assert "--include-relevant" in result.stdout
    assert "--expected-plan" in result.stdout


def test_cli_refuses_manifest_change_during_loading(isolated_db, monkeypatch) -> None:
    from scripts import migrate_legacy_filter as command

    _legacy_job("ML Engineer")
    database = _database(isolated_db)
    manifest = isolated_db / "review.json"
    manifest.write_text("{}", encoding="utf-8")
    expected_plan = isolated_db / "expected-plan.json"
    expected_plan.write_text("{}", encoding="utf-8")
    profile_dir = isolated_db / "profile"
    profile_dir.mkdir()

    def changing_manifest(path):
        path.write_text('{"changed":true}', encoding="utf-8")
        return SimpleNamespace(
            allowed_uncertain_rejected_ids=frozenset(), allowed_rejected_ids=frozenset(),
        )

    before = _snapshot(database)
    monkeypatch.setattr(command, "load_manifest", changing_manifest)
    monkeypatch.setattr(command, "build_rejector", lambda profile: _Rejector())
    monkeypatch.setattr(command, "load_profile", lambda path: None)
    monkeypatch.setattr(sys, "argv", [
        "migrate", "--database", str(database), "--review-manifest", str(manifest),
        "--profile-dir", str(profile_dir), "--apply",
        "--expected-plan", str(expected_plan),
    ])
    with pytest.raises(LegacyFilterMigrationPreconditionError, match="changed while loading"):
        command.main()
    assert _snapshot(database) == before
    assert not list((isolated_db / "backups").glob("*.db"))


def test_plan_hash_is_stable_and_covers_allowlist_and_decisions(isolated_db) -> None:
    _legacy_job("ML Engineer")
    plan = _plan(_database(isolated_db))
    assert plan.plan_sha256 == replace(plan, planned_at="later").plan_sha256
    assert plan.to_dict()["plan_sha256"] == plan.plan_sha256
    assert plan.plan_sha256 != replace(
        plan, allowed_rejected_ids=frozenset({999}),
    ).plan_sha256
    assert plan.plan_sha256 != replace(
        plan, changes=(replace(plan.changes[0], row_fingerprint="changed"),),
    ).plan_sha256


@pytest.mark.parametrize("blocked", [False, True])
def test_nonarchived_manual_decision_and_override_remain_active(isolated_db, blocked) -> None:
    job_id = _legacy_job("Blocked manual" if blocked else "ML manual", components={
        "rejection_stage": "manual", "rejection_reasons": ["manual_archive"],
        "rejection_summary": "manual_archive",
        "reasons": ["Manual override: kept by user", "legacy_unclear"],
    })
    database = _database(isolated_db)
    before = _snapshot(database)
    plan = _plan(database, allowed={job_id} if blocked else set())
    apply_legacy_filter_migration(plan, _Rejector())
    after = _snapshot(database)
    assert after["jobs"] == before["jobs"]
    components = json.loads(after["job_scores"][0]["components"])
    assert components["rejection_stage"] == "manual"
    assert components["rejection_reasons"] == ["manual_archive"]
    assert components["rejection_summary"] == "manual_archive"
    assert "Manual override: kept by user" in components["reasons"]
    assert "legacy_unclear" not in components["reasons"]


def _cli_apply_arguments(isolated_db, monkeypatch, *, allowed=frozenset()):
    from scripts import migrate_legacy_filter as command

    database = _database(isolated_db)
    manifest = isolated_db / "review.json"
    manifest.write_text("{}", encoding="utf-8")
    profile_dir = isolated_db / "profile"
    profile_dir.mkdir()
    expected_plan = isolated_db / "expected-plan.json"
    expected_plan.write_text(json.dumps(_plan(
        database, allowed=set(allowed), review_manifest=manifest,
    ).to_dict()), encoding="utf-8")
    monkeypatch.setattr(command, "load_manifest", lambda path: SimpleNamespace(
        allowed_uncertain_rejected_ids=allowed, allowed_rejected_ids=frozenset(),
    ))
    monkeypatch.setattr(command, "build_rejector", lambda profile: _Rejector())
    monkeypatch.setattr(command, "load_profile", lambda path: None)
    arguments = [
        "migrate", "--database", str(database), "--review-manifest", str(manifest),
        "--profile-dir", str(profile_dir), "--apply", "--expected-plan", str(expected_plan),
    ]
    return command, database, arguments


@pytest.mark.parametrize("drift", ["added_row", "scope", "allowlist", "content"])
def test_cli_refuses_any_change_to_reviewed_plan(isolated_db, monkeypatch, drift) -> None:
    _legacy_job("ML Engineer")
    command, database, arguments = _cli_apply_arguments(isolated_db, monkeypatch)
    if drift == "added_row":
        _legacy_job("Another ML Engineer")
    elif drift == "scope":
        _legacy_job("Relevant ML Engineer", disposition="relevant")
        arguments.append("--include-relevant")
    elif drift == "allowlist":
        monkeypatch.setattr(command, "load_manifest", lambda path: SimpleNamespace(
            allowed_uncertain_rejected_ids=frozenset({999}), allowed_rejected_ids=frozenset(),
        ))
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE jobs SET description='changed content'")
    before = _snapshot(database)
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(LegacyFilterMigrationPreconditionError, match="differs from the reviewed"):
        command.main()
    assert _snapshot(database) == before
    assert not list((isolated_db / "backups").glob("*.db"))


def test_cli_apply_requires_previously_reviewed_plan(isolated_db, monkeypatch, capsys) -> None:
    from scripts import migrate_legacy_filter as command

    monkeypatch.setattr(sys, "argv", [
        "migrate", "--database", str(_database(isolated_db)), "--review-manifest", "none",
        "--apply",
    ])
    with pytest.raises(SystemExit) as exc:
        command.main()
    assert exc.value.code == 2
    assert "--apply requires --expected-plan" in capsys.readouterr().err


def test_report_preflight_failure_prevents_database_mutation(isolated_db, monkeypatch) -> None:
    _legacy_job("ML Engineer")
    command, database, arguments = _cli_apply_arguments(isolated_db, monkeypatch)
    output = isolated_db / "report-directory"
    output.mkdir()
    monkeypatch.setattr(sys, "argv", [*arguments, "--output", str(output)])
    before = _snapshot(database)
    with pytest.raises(IsADirectoryError, match="Report output is a directory"):
        command.main()
    assert _snapshot(database) == before
    assert not list((isolated_db / "backups").glob("*.db"))


def test_report_failure_after_commit_emits_success_receipt(isolated_db, monkeypatch, capsys) -> None:
    _legacy_job("ML Engineer")
    command, database, arguments = _cli_apply_arguments(isolated_db, monkeypatch)
    output = isolated_db / "report.json"
    monkeypatch.setattr(sys, "argv", [*arguments, "--output", str(output)])

    def failing_report(reserved, destination, rendered):
        assert reserved.is_file()
        assert json.loads(_snapshot(database)["job_scores"][0]["components"])[
            "filter_disposition"
        ] == "pending_relevance"
        raise OSError("simulated report destination failure")

    monkeypatch.setattr(command, "_write_report", failing_report)
    command.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["dry_run"] is False
    assert receipt["committed"] is True
    assert receipt["changed_rows"] == 1
    assert receipt["metadata_only_verified"] is True
    assert receipt["report_output_error"] == "simulated report destination failure"
    assert Path(receipt["backup"]).is_file()
    assert not list(isolated_db.glob(".report.json.*.tmp"))


def test_integrity_checks_finish_before_commit(isolated_db, monkeypatch) -> None:
    from smartapply.maintenance import legacy_filter_migration as migration

    _legacy_job("ML Engineer")
    plan = _plan(_database(isolated_db))
    original = migration.require_integrity
    checked = []

    def require_transactional_integrity(connection, *, label):
        assert connection.in_transaction
        checked.append(label)
        return original(connection, label=label)

    monkeypatch.setattr(migration, "require_integrity", require_transactional_integrity)
    report = apply_legacy_filter_migration(plan, _Rejector())
    assert report.post_update_integrity == "ok"
    assert checked == ["source", "updated database before commit"]
