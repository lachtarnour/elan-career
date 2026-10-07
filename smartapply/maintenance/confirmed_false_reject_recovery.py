"""Recover only false rejections proven by the exhaustive audit replay.

This is deliberately separate from the broad historical hard-reject backfill.
Eligibility comes from an immutable replay manifest and is narrowed again by
the current database state.  No relevance classification is performed here:
every recovered row is routed to ``pending_relevance``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from smartapply.filtering import HardRejectResult
from smartapply.filtering.hard_reject.input_fingerprint import (
    hard_reject_input_sha256,
)
from smartapply.filtering.types import HasJobFields
from smartapply.maintenance._sqlite_safety import (
    SQLiteSafetyError,
    canonical_json,
    fingerprint,
    online_backup,
    open_read_only,
    require_integrity,
    require_no_table_triggers,
    resolve_database,
    table_counts,
    utc_now,
)

RULESET_VERSION = "hard-reject-v2"
RECOVERY_COMPONENT = "confirmed_false_rejection_recovery"

_SCORE_COLUMNS = (
    "rule_based_score",
    "semantic_score",
    "skill_score",
    "title_score",
    "seniority_score",
    "location_score",
    "domain_score",
    "final_score",
)
_STALE_REJECTION_KEYS = (
    "rejection_stage",
    "rejection_reasons",
    "rejection_summary",
)
_SCORE_COMPONENT_KEYS = (
    "rule_based",
    "semantic",
    "skills",
    "title",
    "seniority",
    "location",
    "domain",
    "final",
    "cap",
)
_AUDIT_SCOPE_FIELDS = (
    "audited_ids",
    "database_verified_ids",
    "persisted_rejected_ids",
    "semantic_gate_ids",
    "semantic_evaluated_ids",
    "duplicate_identity_only_ids",
    "protected_rejections",
    "certain_false_rejections",
    "probable_false_rejections",
    "ambiguous_semantic",
)


class Rejector(Protocol):
    """The production deterministic rejector interface used by recovery."""

    def evaluate(self, job: HasJobFields) -> HardRejectResult: ...


class ConfirmedFalseRejectRecoveryError(RuntimeError):
    """The recovery manifest or database failed a safety check."""


class ConfirmedFalseRejectPreconditionError(ConfirmedFalseRejectRecoveryError):
    """The manifest or database drifted after the read-only plan."""


@dataclass(frozen=True)
class RecoveryJob:
    """Minimal job shape accepted by the deterministic rejector."""

    id: int
    title: str
    company: str
    description: str
    location: str | None
    contract_type: str | None
    remote_policy: str | None
    application_url: str | None
    source: str
    source_data: dict[str, Any] | None


@dataclass(frozen=True)
class AuditEvidence:
    """The exact replay conclusion needed to authorize one recovery."""

    job_id: int
    title: str
    company: str
    audited_input_sha256: str
    audit_file: str
    audit_decision: str
    classification: str
    classification_reason: str
    old_bucket: str
    old_stage: str
    old_reason: str
    old_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["old_reasons"] = list(self.old_reasons)
        return payload


@dataclass(frozen=True)
class ReplayManifestRequirements:
    """External trust anchor for one exhaustive replay dataset.

    The replay JSON cannot authorize its own scope.  Production therefore
    checks its frozen cohort sizes and the digest of all immutable recovery
    evidence against this independently versioned object.
    """

    name: str
    audited_ids: int
    database_verified_ids: int
    persisted_rejected_ids: int
    semantic_gate_ids: int
    semantic_evaluated_ids: int
    duplicate_identity_only_ids: int
    protected_rejections: int
    certain_false_rejections: int
    probable_false_rejections: int
    ambiguous_semantic: int
    confirmed_evidence_sha256: str

    @classmethod
    def for_fixture_payload(
        cls,
        payload: dict[str, Any],
        *,
        name: str = "isolated-test-fixture",
    ) -> ReplayManifestRequirements:
        """Build an explicit anchor for an isolated test fixture.

        Production callers use ``CANONICAL_REPLAY_REQUIREMENTS``.  This
        constructor keeps small database tests practical without weakening
        the default used by the maintenance command.
        """
        records = payload.get("diff_records")
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ConfirmedFalseRejectRecoveryError(
                "Replay fixture diff_records is invalid"
            )
        entries = tuple(
            _audit_evidence(row) for row in records if _is_confirmed_rescue(row)
        )
        return cls(
            name=name,
            **{field: _integer_field(payload, field) for field in _AUDIT_SCOPE_FIELDS},
            confirmed_evidence_sha256=_confirmed_evidence_digest(entries),
        )

    @property
    def scope_counts(self) -> dict[str, int]:
        return {field: int(getattr(self, field)) for field in _AUDIT_SCOPE_FIELDS}


CANONICAL_REPLAY_REQUIREMENTS = ReplayManifestRequirements(
    name="classification-audit-20261005",
    audited_ids=6763,
    database_verified_ids=6763,
    persisted_rejected_ids=6763,
    semantic_gate_ids=5955,
    semantic_evaluated_ids=5955,
    duplicate_identity_only_ids=808,
    protected_rejections=5665,
    certain_false_rejections=61,
    probable_false_rejections=83,
    ambiguous_semantic=146,
    confirmed_evidence_sha256=(
        "8463bdab6c18a2f610ff34a3c9163b2192863ce5c06473d7ac0aa4a480630bb3"
    ),
)


@dataclass(frozen=True)
class ReplayManifest:
    """Validated, content-addressed audit input."""

    path: Path
    sha256: str
    entries: tuple[AuditEvidence, ...]

    @property
    def by_id(self) -> dict[int, AuditEvidence]:
        return {entry.job_id: entry for entry in self.entries}


@dataclass(frozen=True)
class RecoveryChange:
    """One row approved by the manifest and the read-only database plan."""

    job_id: int
    history_state: str
    row_fingerprint: str
    evidence: AuditEvidence


@dataclass(frozen=True)
class RecoveryPlan:
    database: Path
    replay_manifest: Path
    manifest_sha256: str
    manifest_requirements: ReplayManifestRequirements
    ruleset_version: str
    manifest_candidates: int
    changes: tuple[RecoveryChange, ...]
    excluded_ids: dict[str, tuple[int, ...]]
    planned_at: str

    @property
    def change_ids(self) -> list[int]:
        return [change.job_id for change in self.changes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "replay_manifest": str(self.replay_manifest),
            "manifest_sha256": self.manifest_sha256,
            "manifest_anchor": self.manifest_requirements.name,
            "confirmed_evidence_sha256": (
                self.manifest_requirements.confirmed_evidence_sha256
            ),
            "dry_run": True,
            "ruleset_version": self.ruleset_version,
            "manifest_candidates": self.manifest_candidates,
            "recoverable_rows": len(self.changes),
            "recoverable_ids": self.change_ids,
            "history_states": {
                state: [
                    change.job_id
                    for change in self.changes
                    if change.history_state == state
                ]
                for state in ("fresh", "filtered", "ranked", "analyzed")
            },
            "excluded_counts": {
                reason: len(ids) for reason, ids in sorted(self.excluded_ids.items())
            },
            "excluded_ids": {
                reason: list(ids) for reason, ids in sorted(self.excluded_ids.items())
            },
            "planned_at": self.planned_at,
        }


@dataclass(frozen=True)
class RecoveryReport:
    database: Path
    replay_manifest: Path
    manifest_sha256: str
    backup: Path
    changed_ids: tuple[int, ...]
    fresh_ids: tuple[int, ...]
    history_preserved_ids: tuple[int, ...]
    source_integrity: str
    backup_integrity: str
    post_update_integrity: str
    metadata_only_verified: bool
    table_counts_unchanged: bool
    applied_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "replay_manifest": str(self.replay_manifest),
            "manifest_sha256": self.manifest_sha256,
            "dry_run": False,
            "backup": str(self.backup),
            "ruleset_version": RULESET_VERSION,
            "changed_rows": len(self.changed_ids),
            "changed_ids": list(self.changed_ids),
            "fresh_ids": list(self.fresh_ids),
            "history_preserved_ids": list(self.history_preserved_ids),
            "source_integrity": self.source_integrity,
            "backup_integrity": self.backup_integrity,
            "post_update_integrity": self.post_update_integrity,
            "metadata_only_verified": self.metadata_only_verified,
            "table_counts_unchanged": self.table_counts_unchanged,
            "applied_at": self.applied_at,
        }


def plan_confirmed_false_recovery(
    database: Path | str,
    replay_manifest: Path | str,
    rejector: Rejector,
    *,
    manifest_requirements: ReplayManifestRequirements = CANONICAL_REPLAY_REQUIREMENTS,
) -> RecoveryPlan:
    """Build a read-only recovery plan from confirmed replay evidence."""
    database_path = _database_path(database)
    manifest = load_replay_manifest(
        replay_manifest,
        requirements=manifest_requirements,
    )

    with closing(open_read_only(database_path)) as connection:
        _require_integrity(connection, label="source")
        records = _load_records_by_id(connection, list(manifest.by_id))

    current = {int(record["job__id"]): record for record in records}
    missing = sorted(set(manifest.by_id) - set(current))
    if missing:
        raise ConfirmedFalseRejectRecoveryError(
            f"Replay IDs are missing from the target database: {missing[:20]}"
        )

    excluded: defaultdict[str, list[int]] = defaultdict(list)
    changes: list[RecoveryChange] = []
    for job_id, evidence in sorted(manifest.by_id.items()):
        record = current[job_id]
        exclusion = _exclusion_reason(record, evidence)
        if exclusion is not None:
            excluded[exclusion].append(job_id)
            continue

        result = rejector.evaluate(_job_from_record(record))
        if result.ruleset_version != RULESET_VERSION:
            raise ConfirmedFalseRejectRecoveryError(
                f"Job {job_id}: expected {RULESET_VERSION}, got {result.ruleset_version}"
            )
        if result.rejected:
            excluded["blocked_by_current_hard_reject"].append(job_id)
            continue

        changes.append(
            RecoveryChange(
                job_id=job_id,
                history_state=_history_state(record),
                row_fingerprint=fingerprint(record),
                evidence=evidence,
            )
        )

    return RecoveryPlan(
        database=database_path,
        replay_manifest=manifest.path,
        manifest_sha256=manifest.sha256,
        manifest_requirements=manifest_requirements,
        ruleset_version=RULESET_VERSION,
        manifest_candidates=len(manifest.entries),
        changes=tuple(changes),
        excluded_ids={reason: tuple(ids) for reason, ids in excluded.items()},
        planned_at=utc_now(),
    )


def apply_confirmed_false_recovery(
    plan: RecoveryPlan,
    rejector: Rejector,
    *,
    backup_dir: Path | str | None = None,
) -> RecoveryReport:
    """Apply a plan atomically after rechecking every source of authority."""
    if not plan.changes:
        raise ConfirmedFalseRejectRecoveryError("The plan contains no row to recover")
    if plan.ruleset_version != RULESET_VERSION:
        raise ConfirmedFalseRejectRecoveryError(
            f"Unsupported recovery ruleset: {plan.ruleset_version}"
        )

    database = _database_path(plan.database)
    manifest = load_replay_manifest(
        plan.replay_manifest,
        requirements=plan.manifest_requirements,
    )
    if manifest.sha256 != plan.manifest_sha256:
        raise ConfirmedFalseRejectPreconditionError(
            "The replay manifest changed after planning; rerun the dry-run"
        )
    manifest_entries = manifest.by_id
    for change in plan.changes:
        if manifest_entries.get(change.job_id) != change.evidence:
            raise ConfirmedFalseRejectPreconditionError(
                f"Replay evidence changed for job {change.job_id}; rerun the dry-run"
            )

    destination_dir = (
        Path(backup_dir).expanduser().resolve()
        if backup_dir is not None
        else database.parent / "backups"
    )
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = destination_dir / (
        f"{database.stem}-confirmed-false-reject-recovery-{timestamp}.db"
    )
    applied_at = utc_now()

    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        source_integrity = _require_integrity(connection, label="source")
        current = {
            int(record["job__id"]): record
            for record in _load_records_by_id(connection, plan.change_ids)
        }
        current_results = _validate_current_rows(plan, current, rejector)
        _require_trigger_free_targets(connection)

        counts_before = _table_counts(connection)
        immutable_before = {
            change.job_id: _immutable_fingerprint(
                current[change.job_id], history_state=change.history_state
            )
            for change in plan.changes
        }
        backup_integrity = _online_backup(database, backup)

        changes_before = connection.total_changes
        for change in plan.changes:
            record = current[change.job_id]
            components = _recovered_components(
                record,
                current_results[change.job_id],
                evidence=change.evidence,
                history_state=change.history_state,
                manifest_sha256=plan.manifest_sha256,
                recovered_at=applied_at,
            )
            _update_recovered_row(
                connection,
                change=change,
                components=components,
                recovered_at=applied_at,
            )

        expected_changes = 2 * len(plan.changes)
        actual_changes = connection.total_changes - changes_before
        if actual_changes != expected_changes:
            raise ConfirmedFalseRejectRecoveryError(
                "Metadata-only recovery wrote an unexpected number of rows: "
                f"expected {expected_changes}, got {actual_changes}"
            )

        updated = {
            int(record["job__id"]): record
            for record in _load_records_by_id(connection, plan.change_ids)
        }
        _verify_updated_rows(plan, updated)
        immutable_after = {
            change.job_id: _immutable_fingerprint(
                updated[change.job_id], history_state=change.history_state
            )
            for change in plan.changes
        }
        if immutable_after != immutable_before:
            raise ConfirmedFalseRejectRecoveryError(
                "An immutable job, score, analysis, or application field changed"
            )

        counts_after = _table_counts(connection)
        if counts_after != counts_before:
            raise ConfirmedFalseRejectRecoveryError(
                "A table row count changed during metadata-only recovery"
            )
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ConfirmedFalseRejectRecoveryError(
                f"Foreign-key verification failed: {foreign_key_errors[:5]}"
            )
        _require_integrity(connection, label="pre-commit")
        connection.commit()
        post_update_integrity = _require_integrity(connection, label="post-commit")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    fresh_ids = tuple(
        change.job_id for change in plan.changes if change.history_state == "fresh"
    )
    history_ids = tuple(
        change.job_id for change in plan.changes if change.history_state != "fresh"
    )
    return RecoveryReport(
        database=database,
        replay_manifest=manifest.path,
        manifest_sha256=manifest.sha256,
        backup=backup,
        changed_ids=tuple(plan.change_ids),
        fresh_ids=fresh_ids,
        history_preserved_ids=history_ids,
        source_integrity=source_integrity,
        backup_integrity=backup_integrity,
        post_update_integrity=post_update_integrity,
        metadata_only_verified=True,
        table_counts_unchanged=True,
        applied_at=applied_at,
    )


def load_replay_manifest(
    replay_manifest: Path | str,
    *,
    requirements: ReplayManifestRequirements = CANONICAL_REPLAY_REQUIREMENTS,
) -> ReplayManifest:
    """Validate the exhaustive replay and extract only certain rescued rows."""
    path = Path(replay_manifest).expanduser().resolve()
    if not path.is_file():
        raise ConfirmedFalseRejectRecoveryError(f"Replay manifest does not exist: {path}")
    raw = path.read_bytes()
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfirmedFalseRejectRecoveryError("Replay manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ConfirmedFalseRejectRecoveryError("Replay manifest must be a JSON object")
    if payload.get("ruleset_version") != RULESET_VERSION:
        raise ConfirmedFalseRejectRecoveryError(
            f"Replay manifest must use {RULESET_VERSION}"
        )
    if _integer_field(payload, "true_regression_count") != 0:
        raise ConfirmedFalseRejectRecoveryError(
            "Replay manifest has unresolved true regressions"
        )

    records = payload.get("diff_records")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ConfirmedFalseRejectRecoveryError("Replay manifest diff_records is invalid")
    if _integer_field(payload, "diff_record_count") != len(records):
        raise ConfirmedFalseRejectRecoveryError("Replay diff_record_count is inconsistent")

    _validate_replay_scope(payload, requirements)

    entries: list[AuditEvidence] = []
    for row in records:
        if not _is_confirmed_rescue(row):
            continue
        entries.append(_audit_evidence(row))

    declared_false = _integer_field(payload, "certain_false_rejections")
    declared_rescued = _integer_field(payload, "certain_rescued")
    declared_still_rejected = payload.get("certain_still_rejected_ids")
    if (
        declared_false != len(entries)
        or declared_rescued != len(entries)
        or declared_still_rejected != []
    ):
        raise ConfirmedFalseRejectRecoveryError(
            "Replay summary does not prove that every certain false rejection was rescued"
        )

    ids = [entry.job_id for entry in entries]
    if len(ids) != len(set(ids)):
        raise ConfirmedFalseRejectRecoveryError(
            "Replay contains duplicate confirmed-false-rejection IDs"
        )
    if not entries:
        raise ConfirmedFalseRejectRecoveryError(
            "Replay contains no confirmed false rejection eligible for recovery"
        )
    evidence_sha256 = _confirmed_evidence_digest(tuple(entries))
    if evidence_sha256 != requirements.confirmed_evidence_sha256:
        raise ConfirmedFalseRejectRecoveryError(
            "Replay confirmed-recovery evidence does not match the trusted "
            f"{requirements.name} audit anchor"
        )
    return ReplayManifest(
        path=path,
        sha256=manifest_sha256,
        entries=tuple(sorted(entries, key=lambda entry: entry.job_id)),
    )


def _validate_replay_scope(
    payload: dict[str, Any],
    requirements: ReplayManifestRequirements,
) -> None:
    actual = {field: _integer_field(payload, field) for field in _AUDIT_SCOPE_FIELDS}
    if actual != requirements.scope_counts:
        mismatches = {
            field: {"expected": requirements.scope_counts[field], "actual": actual[field]}
            for field in _AUDIT_SCOPE_FIELDS
            if actual[field] != requirements.scope_counts[field]
        }
        raise ConfirmedFalseRejectRecoveryError(
            "Replay scope does not match the trusted "
            f"{requirements.name} audit anchor: {mismatches}"
        )

    if actual["database_verified_ids"] != actual["audited_ids"]:
        raise ConfirmedFalseRejectRecoveryError(
            "Replay did not verify every audited ID against the frozen database"
        )
    if actual["persisted_rejected_ids"] != actual["audited_ids"]:
        raise ConfirmedFalseRejectRecoveryError(
            "Replay source does not contain the complete persisted rejection set"
        )
    if (
        actual["semantic_gate_ids"] + actual["duplicate_identity_only_ids"]
        != actual["audited_ids"]
    ):
        raise ConfirmedFalseRejectRecoveryError(
            "Replay semantic and identity-only cohorts do not cover the audit"
        )
    if actual["semantic_evaluated_ids"] != actual["semantic_gate_ids"]:
        raise ConfirmedFalseRejectRecoveryError(
            "Replay did not evaluate every semantic-gate ID"
        )
    semantic_cohorts = sum(
        actual[field]
        for field in (
            "protected_rejections",
            "certain_false_rejections",
            "probable_false_rejections",
            "ambiguous_semantic",
        )
    )
    if semantic_cohorts != actual["semantic_gate_ids"]:
        raise ConfirmedFalseRejectRecoveryError(
            "Replay semantic cohorts do not partition the evaluated IDs"
        )


def _confirmed_evidence_digest(entries: tuple[AuditEvidence, ...]) -> str:
    anchored = [
        {
            "id": entry.job_id,
            "title": entry.title,
            "company": entry.company,
            "audited_input_sha256": entry.audited_input_sha256,
            "audit_file": entry.audit_file,
            "audit_decision": entry.audit_decision,
            "classification": entry.classification,
            "classification_reason": entry.classification_reason,
            "old_bucket": entry.old_bucket,
            "old_stage": entry.old_stage,
            "old_reason": entry.old_reason,
            "old_reasons": list(entry.old_reasons),
        }
        for entry in sorted(entries, key=lambda item: item.job_id)
    ]
    return fingerprint(anchored)


def _integer_field(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise ConfirmedFalseRejectRecoveryError(
            f"Replay manifest field {key!r} must be an integer"
        )
    return value


def _is_confirmed_rescue(row: dict[str, Any]) -> bool:
    return (
        row.get("audit_decision") == "certain_false_rejection"
        and row.get("classification") == "confirmed_false_rejection_rescued"
        and row.get("old_disposition") == "rejected"
        and row.get("new_disposition") == "pending_relevance"
        and row.get("new_rejected") is False
    )


def _audit_evidence(row: dict[str, Any]) -> AuditEvidence:
    required_strings = (
        "title",
        "company",
        "audited_input_sha256",
        "audit_file",
        "audit_decision",
        "classification",
        "classification_reason",
        "old_bucket",
        "old_stage",
        "old_reason",
    )
    if any(not isinstance(row.get(key), str) for key in required_strings):
        raise ConfirmedFalseRejectRecoveryError(
            f"Replay record {row.get('id')!r} is missing required audit text"
        )
    old_reasons = row.get("old_reasons")
    if not isinstance(old_reasons, list) or not all(
        isinstance(reason, str) for reason in old_reasons
    ):
        raise ConfirmedFalseRejectRecoveryError(
            f"Replay record {row.get('id')!r} has invalid old_reasons"
        )
    raw_job_id = row.get("id")
    if type(raw_job_id) is not int or raw_job_id <= 0:
        raise ConfirmedFalseRejectRecoveryError("Replay record has an invalid ID")
    job_id = raw_job_id
    return AuditEvidence(
        job_id=job_id,
        title=row["title"],
        company=row["company"],
        audited_input_sha256=row["audited_input_sha256"],
        audit_file=row["audit_file"],
        audit_decision=row["audit_decision"],
        classification=row["classification"],
        classification_reason=row["classification_reason"],
        old_bucket=row["old_bucket"],
        old_stage=row["old_stage"],
        old_reason=row["old_reason"],
        old_reasons=tuple(old_reasons),
    )


def _database_path(database: Path | str) -> Path:
    try:
        return resolve_database(database)
    except SQLiteSafetyError as exc:
        raise ConfirmedFalseRejectRecoveryError(str(exc)) from exc


def _require_integrity(connection: sqlite3.Connection, *, label: str) -> str:
    try:
        return require_integrity(connection, label=label)
    except SQLiteSafetyError as exc:
        raise ConfirmedFalseRejectRecoveryError(str(exc)) from exc


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return table_counts(connection)


def _online_backup(database: Path, backup: Path) -> str:
    try:
        return online_backup(database, backup)
    except SQLiteSafetyError as exc:
        raise ConfirmedFalseRejectRecoveryError(str(exc)) from exc


def _require_trigger_free_targets(connection: sqlite3.Connection) -> None:
    try:
        require_no_table_triggers(connection, tables=("jobs", "job_scores"))
    except SQLiteSafetyError as exc:
        raise ConfirmedFalseRejectRecoveryError(str(exc)) from exc


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _record_query(connection: sqlite3.Connection, where: str) -> str:
    job_columns = _table_columns(connection, "jobs")
    score_columns = _table_columns(connection, "job_scores")
    if not job_columns or not score_columns:
        raise ConfirmedFalseRejectRecoveryError("Required jobs/job_scores tables are missing")
    projections = [f'j."{name}" AS "job__{name}"' for name in job_columns]
    projections.extend(f's."{name}" AS "score__{name}"' for name in score_columns)
    projections.extend(
        (
            "(SELECT COUNT(*) FROM applications a WHERE a.job_id = j.id) AS application_count",
            "(SELECT COUNT(*) FROM job_analyses a WHERE a.job_id = j.id) AS analysis_count",
        )
    )
    return (
        f"SELECT {', '.join(projections)} "
        "FROM jobs j JOIN job_scores s ON s.job_id = j.id "
        f"WHERE {where} ORDER BY j.id"
    )


def _load_records_by_id(
    connection: sqlite3.Connection,
    job_ids: list[int],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for start in range(0, len(job_ids), 900):
        batch = job_ids[start : start + 900]
        placeholders = ",".join("?" for _ in batch)
        query = _record_query(connection, f"j.id IN ({placeholders})")
        records.extend(dict(row) for row in connection.execute(query, batch))
    return records


def _decode_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConfirmedFalseRejectRecoveryError("A persisted JSON object is invalid") from exc
    if not isinstance(decoded, dict):
        raise ConfirmedFalseRejectRecoveryError("A persisted JSON value must be an object")
    return decoded


def _job_from_record(record: dict[str, Any]) -> RecoveryJob:
    source_data = _decode_object(record.get("job__source_data"))
    return RecoveryJob(
        id=int(record["job__id"]),
        title=str(record.get("job__title") or ""),
        company=str(record.get("job__company") or ""),
        description=str(record.get("job__description") or ""),
        location=record.get("job__location"),
        contract_type=record.get("job__contract_type"),
        remote_policy=record.get("job__remote_policy"),
        application_url=record.get("job__application_url"),
        source=str(record.get("job__source") or ""),
        source_data=source_data or None,
    )


def _exclusion_reason(
    record: dict[str, Any],
    evidence: AuditEvidence,
) -> str | None:
    if int(record.get("application_count") or 0):
        return "has_application"
    if record.get("job__canonical_job_id") is not None:
        return "confirmed_duplicate_alias"
    if record.get("job__possible_duplicate_of_id") is not None:
        return "pending_duplicate_review"
    if record.get("job__title") != evidence.title or record.get("job__company") != evidence.company:
        return "audit_identity_mismatch"
    if hard_reject_input_sha256(_job_from_record(record)) != evidence.audited_input_sha256:
        return "audit_input_mismatch"

    components = _decode_object(record.get("score__components"))
    disposition = components.get("filter_disposition")
    if disposition == "pending_relevance" and record.get("job__archived_at") is None:
        return "already_recovered"
    if (
        disposition != "rejected"
        or record.get("job__status") != "archived"
        or record.get("job__archived_at") is None
    ):
        return "inconsistent_rejection_state"
    if (
        components.get("rejection_stage") != evidence.old_stage
        or tuple(components.get("rejection_reasons", []) or ()) != evidence.old_reasons
        or components.get("rejection_summary") != evidence.old_reason
    ):
        return "audit_rejection_mismatch"
    if record.get("job__shortlisted_at") is not None or record.get("job__shortlist_origin") is not None:
        return "has_shortlist_state"

    duplicate_status = record.get("job__duplicate_review_status") or "none"
    if duplicate_status not in {"none", "rejected", "confirmed"}:
        return "unsupported_duplicate_state"
    try:
        _history_state(record)
    except ConfirmedFalseRejectRecoveryError:
        return "inconsistent_pipeline_history"
    return None


def _history_state(record: dict[str, Any]) -> str:
    filtered_at = record.get("job__filtered_at")
    ranked_at = record.get("job__ranked_at")
    analyzed_at = record.get("job__analyzed_at")
    analysis_count = int(record.get("analysis_count") or 0)
    if analysis_count not in {0, 1} or bool(analyzed_at) != bool(analysis_count):
        raise ConfirmedFalseRejectRecoveryError("Inconsistent analysis timestamp/row state")
    if analyzed_at is not None:
        if filtered_at is None or ranked_at is None:
            raise ConfirmedFalseRejectRecoveryError("Analyzed row is missing prior timestamps")
        return "analyzed"
    if ranked_at is not None:
        if filtered_at is None:
            raise ConfirmedFalseRejectRecoveryError("Ranked row is missing filtered_at")
        return "ranked"
    if filtered_at is not None:
        return "filtered"
    return "fresh"


def _validate_current_rows(
    plan: RecoveryPlan,
    current: dict[int, dict[str, Any]],
    rejector: Rejector,
) -> dict[int, HardRejectResult]:
    expected_ids = set(plan.change_ids)
    if set(current) != expected_ids:
        missing = sorted(expected_ids - set(current))
        raise ConfirmedFalseRejectPreconditionError(f"Planned jobs disappeared: {missing}")

    results: dict[int, HardRejectResult] = {}
    for change in plan.changes:
        record = current[change.job_id]
        if fingerprint(record) != change.row_fingerprint:
            raise ConfirmedFalseRejectPreconditionError(
                f"Job {change.job_id} changed after planning; rerun the dry-run"
            )
        exclusion = _exclusion_reason(record, change.evidence)
        if exclusion is not None:
            raise ConfirmedFalseRejectPreconditionError(
                f"Job {change.job_id} is no longer eligible: {exclusion}"
            )
        if _history_state(record) != change.history_state:
            raise ConfirmedFalseRejectPreconditionError(
                f"Job {change.job_id} pipeline history changed after planning"
            )
        result = rejector.evaluate(_job_from_record(record))
        if result.ruleset_version != RULESET_VERSION or result.rejected:
            raise ConfirmedFalseRejectPreconditionError(
                f"Job {change.job_id} no longer passes {RULESET_VERSION}"
            )
        results[change.job_id] = result
    return results


def _recovered_components(
    record: dict[str, Any],
    result: HardRejectResult,
    *,
    evidence: AuditEvidence,
    history_state: str,
    manifest_sha256: str,
    recovered_at: str,
) -> dict[str, Any]:
    if result.ruleset_version != RULESET_VERSION or result.rejected:
        raise ConfirmedFalseRejectRecoveryError(
            "Only a current hard-reject-v2 survivor can be recovered"
        )
    previous = _decode_object(record.get("score__components"))
    components = dict(previous)
    for key in _STALE_REJECTION_KEYS:
        components.pop(key, None)
    # Deterministic rejection no longer owns a numeric score. Remove the
    # legacy rule score even when costly ranking/analysis history is retained.
    components.pop("rule_based", None)
    if history_state in {"fresh", "filtered"}:
        for key in _SCORE_COMPONENT_KEYS:
            components.pop(key, None)
    components["reasons"] = list(result.reasons)
    components["filter_disposition"] = "pending_relevance"
    components["hard_reject"] = result.audit_payload()
    components[RECOVERY_COMPONENT] = {
        "schema_version": 1,
        "recovered_at": recovered_at,
        "ruleset_version": RULESET_VERSION,
        "manifest_sha256": manifest_sha256,
        "audit_evidence": evidence.to_dict(),
        "previous_rejection": {
            "status": record.get("job__status"),
            "archived_at": record.get("job__archived_at"),
            "filter_disposition": previous.get("filter_disposition"),
            "stage": previous.get("rejection_stage"),
            "reasons": list(previous.get("rejection_reasons", []) or []),
            "summary": previous.get("rejection_summary"),
            "active_reasons": list(previous.get("reasons", []) or []),
        },
    }
    return components


def _update_recovered_row(
    connection: sqlite3.Connection,
    *,
    change: RecoveryChange,
    components: dict[str, Any],
    recovered_at: str,
) -> None:
    status = "analyzed" if change.history_state == "analyzed" else "filtered"
    if change.history_state == "fresh":
        job_update = connection.execute(
            """
            UPDATE jobs
            SET filtered_at = ?, archived_at = NULL, status = 'filtered'
            WHERE id = ?
            """,
            (recovered_at, change.job_id),
        )
    else:
        job_update = connection.execute(
            "UPDATE jobs SET archived_at = NULL, status = ? WHERE id = ?",
            (status, change.job_id),
        )
    if job_update.rowcount != 1:
        raise ConfirmedFalseRejectRecoveryError(
            f"Expected to update one jobs row for {change.job_id}"
        )

    if change.history_state in {"fresh", "filtered"}:
        assignments = ", ".join(f"{column} = NULL" for column in _SCORE_COLUMNS)
        score_update = connection.execute(
            f"""
            UPDATE job_scores
            SET {assignments}, components = ?, computed_at = ?
            WHERE job_id = ?
            """,
            (canonical_json(components), recovered_at, change.job_id),
        )
    else:
        score_update = connection.execute(
            """
            UPDATE job_scores
            SET rule_based_score = NULL, components = ?
            WHERE job_id = ?
            """,
            (canonical_json(components), change.job_id),
        )
    if score_update.rowcount != 1:
        raise ConfirmedFalseRejectRecoveryError(
            f"Expected to update one job_scores row for {change.job_id}"
        )


def _immutable_fingerprint(record: dict[str, Any], *, history_state: str) -> str:
    mutable_job = {"archived_at", "status"}
    mutable_score = {"components", "rule_based_score"}
    if history_state in {"fresh", "filtered"}:
        mutable_score.update(_SCORE_COLUMNS)
        mutable_score.add("computed_at")
    if history_state == "fresh":
        mutable_job.add("filtered_at")
    immutable = {
        key: value
        for key, value in record.items()
        if not (
            key.startswith("job__") and key.removeprefix("job__") in mutable_job
        )
        and not (
            key.startswith("score__") and key.removeprefix("score__") in mutable_score
        )
    }
    return fingerprint(immutable)


def _verify_updated_rows(
    plan: RecoveryPlan,
    records: dict[int, dict[str, Any]],
) -> None:
    if set(records) != set(plan.change_ids):
        raise ConfirmedFalseRejectRecoveryError(
            "Post-update verification could not reload every changed row"
        )
    changes = {change.job_id: change for change in plan.changes}
    for job_id, record in records.items():
        change = changes[job_id]
        components = _decode_object(record.get("score__components"))
        recovery = components.get(RECOVERY_COMPONENT, {})
        expected_status = "analyzed" if change.history_state == "analyzed" else "filtered"
        invalid = (
            record.get("job__status") != expected_status
            or record.get("job__archived_at") is not None
            or record.get("job__filtered_at") is None
            or components.get("filter_disposition") != "pending_relevance"
            or components.get("hard_reject", {}).get("ruleset_version") != RULESET_VERSION
            or components.get("hard_reject", {}).get("rejected") is not False
            or recovery.get("manifest_sha256") != plan.manifest_sha256
            or recovery.get("audit_evidence", {}).get("job_id") != job_id
            or any(key in components for key in _STALE_REJECTION_KEYS)
            or record.get("score__rule_based_score") is not None
            or "rule_based" in components
        )
        if change.history_state in {"fresh", "filtered"}:
            invalid = invalid or any(
                record.get(f"score__{column}") is not None for column in _SCORE_COLUMNS
            )
            invalid = invalid or any(key in components for key in _SCORE_COMPONENT_KEYS)
        if invalid:
            raise ConfirmedFalseRejectRecoveryError(
                f"Post-update state is invalid for job {job_id}"
            )


__all__ = [
    "AuditEvidence",
    "CANONICAL_REPLAY_REQUIREMENTS",
    "ConfirmedFalseRejectPreconditionError",
    "ConfirmedFalseRejectRecoveryError",
    "RecoveryChange",
    "RecoveryPlan",
    "RecoveryReport",
    "ReplayManifestRequirements",
    "apply_confirmed_false_recovery",
    "load_replay_manifest",
    "plan_confirmed_false_recovery",
]
