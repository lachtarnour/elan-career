"""Safely replay the deterministic rejector on historical local-filter rejects.

This module deliberately does not classify relevance.  It only moves an old
``local_filter`` rejection to ``pending_relevance`` when every current
hard-rejection gate clears the offer.

The public workflow has two explicit steps:

1. :func:`plan_backfill` reads and evaluates eligible rows without writing.
2. :func:`apply_backfill` takes an online backup, rechecks every precondition,
   re-evaluates the selected rows, and commits all metadata changes atomically.

Rows with applications, confirmed or pending duplicate relationships, manual
validation decisions, prior analysis, or inconsistent lifecycle state are
never eligible for this automatic repair.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from smartapply.filtering import (
    CandidateQualifications,
    DeterministicRejector,
    HardRejectResult,
    ruleset_from_preferences,
)
from smartapply.filtering.types import HasJobFields
from smartapply.maintenance._sqlite_safety import (
    SQLiteSafetyError,
    online_backup,
    require_no_table_triggers,
)
from smartapply.pipeline.process.score_components import replace_filter_components
from smartapply.profile import Profile, get_profile

_EXPECTED_RULESET_VERSION = "hard-reject-v2"
_MUTABLE_JOB_COLUMNS = {
    "filtered_at",
    "ranked_at",
    "analyzed_at",
    "archived_at",
    "status",
    "shortlisted_at",
    "shortlist_origin",
}
_MUTABLE_SCORE_COLUMNS = {"components", "computed_at"}


class Rejector(Protocol):
    """Narrow interface used to make the database operation testable."""

    def evaluate(self, job: HasJobFields) -> HardRejectResult: ...


class BackfillError(RuntimeError):
    """Base error for a refused or failed backfill."""


class BackfillPreconditionError(BackfillError):
    """The database changed after planning, so no mutation was attempted."""


@dataclass(frozen=True)
class BackfillJob:
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
class PlannedChange:
    job_id: int
    row_fingerprint: str
    previous_rejection: dict[str, Any]


@dataclass(frozen=True)
class BackfillPlan:
    database: Path
    ruleset_version: str
    rejected_rows: int
    eligible_rows: int
    still_rejected_rows: int
    changes: tuple[PlannedChange, ...]
    exclusion_counts: dict[str, int]
    planned_at: str

    @property
    def change_ids(self) -> list[int]:
        return [change.job_id for change in self.changes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "dry_run": True,
            "ruleset_version": self.ruleset_version,
            "rejected_rows": self.rejected_rows,
            "eligible_rows": self.eligible_rows,
            "still_rejected_rows": self.still_rejected_rows,
            "pending_relevance_rows": len(self.changes),
            "pending_relevance_ids": self.change_ids,
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
            "planned_at": self.planned_at,
        }


@dataclass(frozen=True)
class BackfillReport:
    database: Path
    backup: Path
    ruleset_version: str
    changed_ids: tuple[int, ...]
    source_integrity: str
    backup_integrity: str
    post_update_integrity: str
    metadata_only_verified: bool
    table_counts_unchanged: bool
    applied_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "dry_run": False,
            "backup": str(self.backup),
            "ruleset_version": self.ruleset_version,
            "changed_rows": len(self.changed_ids),
            "changed_ids": list(self.changed_ids),
            "source_integrity": self.source_integrity,
            "backup_integrity": self.backup_integrity,
            "post_update_integrity": self.post_update_integrity,
            "metadata_only_verified": self.metadata_only_verified,
            "table_counts_unchanged": self.table_counts_unchanged,
            "applied_at": self.applied_at,
        }


def build_rejector(profile: Profile | None = None) -> DeterministicRejector:
    """Build the same deterministic rejector used by the production pipeline."""
    candidate = profile or get_profile()
    return DeterministicRejector(
        ruleset_from_preferences(candidate.preferences),
        qualifications=CandidateQualifications.from_profile(candidate),
    )


def plan_backfill(database: Path | str, rejector: Rejector) -> BackfillPlan:
    """Evaluate every safely eligible historical rejection without writing."""
    path = _database_path(database)
    with closing(_read_connection(path)) as connection:
        _require_integrity(connection, label="source")
        records = _load_rejected_records(connection)

    exclusion_counts: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    for record in records:
        exclusion = _exclusion_reason(record)
        if exclusion is None:
            eligible.append(record)
        else:
            exclusion_counts[exclusion] += 1

    changes: list[PlannedChange] = []
    still_rejected = 0
    versions: set[str] = set()
    for record in eligible:
        result = rejector.evaluate(_job_from_record(record))
        versions.add(result.ruleset_version)
        if result.rejected:
            still_rejected += 1
            continue
        changes.append(
            PlannedChange(
                job_id=int(record["job__id"]),
                row_fingerprint=_row_fingerprint(record),
                previous_rejection=_previous_rejection(record),
            )
        )

    if versions and versions != {_EXPECTED_RULESET_VERSION}:
        raise BackfillError(
            f"Expected only {_EXPECTED_RULESET_VERSION!r}, got {sorted(versions)!r}"
        )

    return BackfillPlan(
        database=path,
        ruleset_version=_EXPECTED_RULESET_VERSION,
        rejected_rows=len(records),
        eligible_rows=len(eligible),
        still_rejected_rows=still_rejected,
        changes=tuple(changes),
        exclusion_counts=dict(exclusion_counts),
        planned_at=_utc_now(),
    )


def apply_backfill(
    plan: BackfillPlan,
    rejector: Rejector,
    *,
    backup_dir: Path | str | None = None,
) -> BackfillReport:
    """Apply a plan atomically after backup, drift checks, and a fresh replay."""
    database = _database_path(plan.database)
    if not plan.changes:
        raise BackfillError("The plan contains no row to update")
    if plan.ruleset_version != _EXPECTED_RULESET_VERSION:
        raise BackfillError(f"Unsupported ruleset: {plan.ruleset_version}")

    destination_dir = (
        Path(backup_dir).expanduser().resolve()
        if backup_dir is not None
        else database.parent / "backups"
    )
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    applied_at = _utc_now()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = destination_dir / f"{database.stem}-hard-reject-v2-{timestamp}.db"

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

        table_counts_before = _table_counts(connection)
        immutable_before = {
            job_id: _immutable_fingerprint(record)
            for job_id, record in current.items()
        }
        backup_integrity = _backup_database_locked(database, backup)

        changes_before = connection.total_changes
        for change in plan.changes:
            components = _pending_components(
                current_results[change.job_id],
                previous_components=_decode_object(
                    current[change.job_id].get("score__components")
                ),
                previous_rejection=change.previous_rejection,
                migrated_at=applied_at,
            )
            _update_row(
                connection,
                job_id=change.job_id,
                migrated_at=applied_at,
                components=components,
            )

        expected_changes = 2 * len(plan.changes)
        actual_changes = connection.total_changes - changes_before
        if actual_changes != expected_changes:
            raise BackfillError(
                "Metadata-only backfill wrote an unexpected number of rows: "
                f"expected {expected_changes}, got {actual_changes}"
            )

        updated = {
            int(record["job__id"]): record
            for record in _load_records_by_id(connection, plan.change_ids)
        }
        _verify_updated_rows(plan, updated)
        immutable_after = {
            job_id: _immutable_fingerprint(record)
            for job_id, record in updated.items()
        }
        if immutable_after != immutable_before:
            raise BackfillError("An immutable job or score field changed during backfill")
        table_counts_after = _table_counts(connection)
        if table_counts_after != table_counts_before:
            raise BackfillError("A table row count changed during metadata-only backfill")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise BackfillError(f"Foreign-key verification failed: {foreign_key_errors[:5]}")
        _require_integrity(connection, label="pre-commit")
        connection.commit()
        post_update_integrity = _require_integrity(connection, label="post-commit")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return BackfillReport(
        database=database,
        backup=backup,
        ruleset_version=plan.ruleset_version,
        changed_ids=tuple(plan.change_ids),
        source_integrity=source_integrity,
        backup_integrity=backup_integrity,
        post_update_integrity=post_update_integrity,
        metadata_only_verified=True,
        table_counts_unchanged=True,
        applied_at=applied_at,
    )


def _database_path(database: Path | str) -> Path:
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise BackfillError(f"Database does not exist: {path}")
    return path


def _read_connection(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _record_query(connection: sqlite3.Connection, where: str) -> str:
    job_columns = _table_columns(connection, "jobs")
    score_columns = _table_columns(connection, "job_scores")
    if not job_columns or not score_columns:
        raise BackfillError("Required jobs/job_scores tables are missing")
    projections = [
        f'j."{name}" AS "job__{("company" if name == "source_company" else name)}"'
        for name in job_columns
    ]
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


def _load_rejected_records(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = _record_query(
        connection,
        "json_extract(s.components, '$.filter_disposition') = 'rejected'",
    )
    return [dict(row) for row in connection.execute(query)]


def _load_records_by_id(
    connection: sqlite3.Connection,
    job_ids: list[int],
) -> list[dict[str, Any]]:
    if not job_ids:
        return []
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
        raise BackfillError("A persisted JSON object is invalid") from exc
    if not isinstance(decoded, dict):
        raise BackfillError("A persisted JSON value must be an object")
    return decoded


def _job_from_record(record: dict[str, Any]) -> BackfillJob:
    source_data = _decode_object(record.get("job__source_data"))
    return BackfillJob(
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


def _exclusion_reason(record: dict[str, Any]) -> str | None:
    components = _decode_object(record.get("score__components"))
    if components.get("rejection_stage") != "local_filter":
        return "non_local_filter_decision"
    if record.get("job__status") != "archived" or record.get("job__archived_at") is None:
        return "inconsistent_archive_state"
    if record.get("job__canonical_job_id") is not None:
        return "confirmed_duplicate_alias"
    if record.get("job__possible_duplicate_of_id") is not None:
        return "pending_duplicate_review"
    duplicate_status = record.get("job__duplicate_review_status") or "none"
    if duplicate_status not in {"none", "rejected"}:
        return "confirmed_duplicate_group"
    if int(record.get("application_count") or 0):
        return "has_application"
    if int(record.get("analysis_count") or 0):
        return "has_prior_analysis"
    if any(
        record.get(f"job__{field}") is not None
        for field in ("filtered_at", "ranked_at", "analyzed_at", "shortlisted_at")
    ) or record.get("job__shortlist_origin") is not None:
        return "has_prior_pipeline_state"
    return None


def _previous_rejection(record: dict[str, Any]) -> dict[str, Any]:
    components = _decode_object(record.get("score__components"))
    return {
        "stage": components.get("rejection_stage"),
        "reasons": list(components.get("rejection_reasons", []) or []),
        "summary": components.get("rejection_summary"),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _row_fingerprint(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _immutable_fingerprint(record: dict[str, Any]) -> str:
    immutable = {
        key: value
        for key, value in record.items()
        if not (
            key.startswith("job__") and key.removeprefix("job__") in _MUTABLE_JOB_COLUMNS
        )
        and not (
            key.startswith("score__") and key.removeprefix("score__") in _MUTABLE_SCORE_COLUMNS
        )
    }
    return _row_fingerprint(immutable)


def _validate_current_rows(
    plan: BackfillPlan,
    current: dict[int, dict[str, Any]],
    rejector: Rejector,
) -> dict[int, HardRejectResult]:
    expected_ids = set(plan.change_ids)
    if set(current) != expected_ids:
        missing = sorted(expected_ids - set(current))
        raise BackfillPreconditionError(f"Planned jobs disappeared: {missing}")
    results: dict[int, HardRejectResult] = {}
    for change in plan.changes:
        record = current[change.job_id]
        if _row_fingerprint(record) != change.row_fingerprint:
            raise BackfillPreconditionError(
                f"Job {change.job_id} changed after the dry-run plan; rerun the plan"
            )
        exclusion = _exclusion_reason(record)
        if exclusion is not None:
            raise BackfillPreconditionError(
                f"Job {change.job_id} is no longer eligible: {exclusion}"
            )
        result = rejector.evaluate(_job_from_record(record))
        if result.ruleset_version != plan.ruleset_version or result.rejected:
            raise BackfillPreconditionError(
                f"Job {change.job_id} no longer passes {plan.ruleset_version}"
            )
        results[change.job_id] = result
    return results


def _pending_components(
    result: HardRejectResult,
    *,
    previous_components: dict[str, Any],
    previous_rejection: dict[str, Any],
    migrated_at: str,
) -> dict[str, Any]:
    if result.rejected or result.ruleset_version != _EXPECTED_RULESET_VERSION:
        raise BackfillError("Only a current hard-reject-v2 survivor can be migrated")
    current = {
        "reasons": list(result.reasons),
        "filter_disposition": result.disposition.value,
        "hard_reject": result.audit_payload(),
        "hard_reject_backfill": {
            "migrated_at": migrated_at,
            "previous_disposition": "rejected",
            "previous_rejection": previous_rejection,
        },
    }
    return replace_filter_components(previous_components, current)


def _update_row(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    migrated_at: str,
    components: dict[str, Any],
) -> None:
    job_update = connection.execute(
        """
        UPDATE jobs
        SET filtered_at = ?, ranked_at = NULL, analyzed_at = NULL,
            archived_at = NULL, status = 'filtered', shortlisted_at = NULL,
            shortlist_origin = NULL
        WHERE id = ?
        """,
        (migrated_at, job_id),
    )
    if job_update.rowcount != 1:
        raise BackfillError(f"Expected to update one jobs row for {job_id}")
    score_update = connection.execute(
        """
        UPDATE job_scores
        SET components = ?, computed_at = ?
        WHERE job_id = ?
        """,
        (_canonical_json(components), migrated_at, job_id),
    )
    if score_update.rowcount != 1:
        raise BackfillError(f"Expected to update one job_scores row for {job_id}")


def _verify_updated_rows(plan: BackfillPlan, records: dict[int, dict[str, Any]]) -> None:
    if set(records) != set(plan.change_ids):
        raise BackfillError("Post-update verification could not reload every changed row")
    for job_id, record in records.items():
        components = _decode_object(record.get("score__components"))
        if (
            record.get("job__status") != "filtered"
            or record.get("job__archived_at") is not None
            or record.get("job__filtered_at") is None
            or record.get("job__ranked_at") is not None
            or record.get("job__analyzed_at") is not None
            or record.get("job__shortlisted_at") is not None
            or components.get("filter_disposition") != "pending_relevance"
            or components.get("hard_reject", {}).get("ruleset_version")
            != plan.ruleset_version
            or components.get("hard_reject", {}).get("rejected") is not False
        ):
            raise BackfillError(f"Post-update state is invalid for job {job_id}")


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }


def _require_integrity(connection: sqlite3.Connection, *, label: str) -> str:
    result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if result.lower() != "ok":
        raise BackfillError(f"{label} integrity_check failed: {result}")
    return result


def _backup_database_locked(database: Path, backup: Path) -> str:
    """Create a WAL-aware online backup while the caller holds a write lock."""
    try:
        return online_backup(database, backup)
    except SQLiteSafetyError as exc:
        raise BackfillError(str(exc)) from exc


def _require_trigger_free_targets(connection: sqlite3.Connection) -> None:
    try:
        require_no_table_triggers(connection, tables=("jobs", "job_scores"))
    except SQLiteSafetyError as exc:
        raise BackfillError(str(exc)) from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "BackfillError",
    "BackfillPlan",
    "BackfillPreconditionError",
    "BackfillReport",
    "apply_backfill",
    "build_rejector",
    "plan_backfill",
]
