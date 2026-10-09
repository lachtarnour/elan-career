"""Replay legacy filter labels without destroying paid or manual history.

The migration owns only filter components. A new deterministic blocker can
archive an untouched offer, while existing analyses, applications, duplicate
decisions and manual actions retain their lifecycle. No pipeline is restarted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from smartapply.filtering import HardRejectResult
from smartapply.filtering.types import HasJobFields
from smartapply.maintenance._sqlite_safety import (
    canonical_json,
    file_sha256,
    fingerprint,
    online_backup,
    open_read_only,
    require_integrity,
    require_no_table_triggers,
    resolve_database,
    utc_now,
)
from smartapply.pipeline.process.score_components import FILTER_COMPONENT_KEYS

RULESET_VERSION = "hard-reject-v2"
MIGRATION_COMPONENT = "legacy_filter_migration"
_REJECTION_KEYS = frozenset({"rejection_stage", "rejection_reasons", "rejection_summary"})


class Rejector(Protocol):
    """Only deterministic evaluation is available to this operation."""

    def evaluate(self, job: HasJobFields) -> HardRejectResult: ...


class LegacyFilterMigrationError(RuntimeError):
    """A reviewed migration could not be planned or safely applied."""


class LegacyFilterMigrationPreconditionError(LegacyFilterMigrationError):
    """The reviewed plan no longer describes the current database."""


@dataclass(frozen=True)
class LegacyJob:
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
class LegacyFilterChange:
    job_id: int
    row_fingerprint: str
    decision_fingerprint: str
    rejected: bool
    archive: bool
    lifecycle_protections: tuple[str, ...]


@dataclass(frozen=True)
class LegacyFilterMigrationPlan:
    database: Path
    dispositions: tuple[str, ...]
    allowed_rejected_ids: frozenset[int]
    changes: tuple[LegacyFilterChange, ...]
    excluded_existing_hard_reject: int
    review_manifest: Path | None
    review_manifest_sha256: str | None
    planned_at: str

    @property
    def change_ids(self) -> tuple[int, ...]:
        return tuple(change.job_id for change in self.changes)

    @property
    def plan_sha256(self) -> str:
        """Bind reviewed input, scope and exact decisions without clock noise."""
        return fingerprint({
            "database": str(self.database),
            "dispositions": sorted(self.dispositions),
            "review_manifest_sha256": self.review_manifest_sha256,
            "allowed_rejected_ids": sorted(self.allowed_rejected_ids),
            "changes": [asdict(change) for change in sorted(
                self.changes, key=lambda change: change.job_id,
            )],
            "excluded_existing_hard_reject": self.excluded_existing_hard_reject,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "dry_run": True,
            "plan_sha256": self.plan_sha256,
            "ruleset_version": RULESET_VERSION,
            "legacy_dispositions": list(self.dispositions),
            "review_manifest": str(self.review_manifest) if self.review_manifest else None,
            "review_manifest_sha256": self.review_manifest_sha256,
            "planned_rows": len(self.changes),
            "pending_relevance_rows": sum(not change.rejected for change in self.changes),
            "rejected_rows": sum(change.rejected for change in self.changes),
            "newly_archived_rows": sum(change.archive for change in self.changes),
            "lifecycle_preserved_rejected_rows": sum(
                change.rejected and not change.archive for change in self.changes
            ),
            "protection_counts": dict(sorted(Counter(
                protection
                for change in self.changes
                if change.rejected
                for protection in change.lifecycle_protections
            ).items())),
            "excluded_existing_hard_reject": self.excluded_existing_hard_reject,
            "planned_at": self.planned_at,
        }


@dataclass(frozen=True)
class LegacyFilterMigrationReport:
    database: Path
    backup: Path
    changed_ids: tuple[int, ...]
    newly_archived_ids: tuple[int, ...]
    rejected_rows: int
    source_integrity: str
    backup_integrity: str
    post_update_integrity: str
    applied_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "backup": str(self.backup),
            "dry_run": False,
            "committed": True,
            "ruleset_version": RULESET_VERSION,
            "changed_rows": len(self.changed_ids),
            "rejected_rows": self.rejected_rows,
            "pending_relevance_rows": len(self.changed_ids) - self.rejected_rows,
            "newly_archived_rows": len(self.newly_archived_ids),
            "lifecycle_preserved_rejected_rows": self.rejected_rows - len(self.newly_archived_ids),
            "source_integrity": self.source_integrity,
            "backup_integrity": self.backup_integrity,
            "post_update_integrity": self.post_update_integrity,
            "metadata_only_verified": True,
            "all_other_tables_unchanged": True,
            "applied_at": self.applied_at,
        }


def plan_legacy_filter_migration(
    database: Path | str,
    rejector: Rejector,
    *,
    allowed_rejected_ids: frozenset[int],
    dispositions: tuple[str, ...] = ("uncertain",),
    review_manifest: Path | str | None = None,
    expected_review_manifest_sha256: str | None = None,
) -> LegacyFilterMigrationPlan:
    """Read legacy rows and refuse every blocker outside the reviewed set."""
    _validate_scope(dispositions, allowed_rejected_ids)
    path = resolve_database(database)
    manifest = Path(review_manifest).expanduser().resolve() if review_manifest else None
    manifest_sha256 = file_sha256(manifest) if manifest else None
    if expected_review_manifest_sha256 is not None and (
        manifest_sha256 != expected_review_manifest_sha256
    ):
        raise LegacyFilterMigrationPreconditionError("Review manifest changed while loading")
    with closing(open_read_only(path)) as connection:
        require_integrity(connection, label="source")
        placeholders = ",".join("?" for _ in dispositions)
        records = _load_records(
            connection,
            f"json_extract(s.components, '$.filter_disposition') IN ({placeholders})",
            dispositions,
        )
    changes: list[LegacyFilterChange] = []
    excluded = 0
    for record in records:
        components = _decode_object(record["score__components"])
        if components.get("hard_reject") is not None:
            excluded += 1
            continue
        result = _evaluate(record, rejector, allowed_rejected_ids)
        protections = _lifecycle_protections(record)
        changes.append(LegacyFilterChange(
            job_id=int(record["job__id"]),
            row_fingerprint=fingerprint(record),
            decision_fingerprint=_decision_fingerprint(result),
            rejected=result.rejected,
            archive=result.rejected and not protections,
            lifecycle_protections=protections,
        ))
    return LegacyFilterMigrationPlan(
        database=path,
        dispositions=dispositions,
        allowed_rejected_ids=allowed_rejected_ids,
        changes=tuple(changes),
        excluded_existing_hard_reject=excluded,
        review_manifest=manifest,
        review_manifest_sha256=manifest_sha256,
        planned_at=utc_now(),
    )


def apply_legacy_filter_migration(
    plan: LegacyFilterMigrationPlan,
    rejector: Rejector,
    *,
    backup_dir: Path | str | None = None,
) -> LegacyFilterMigrationReport:
    """Backup, revalidate and update exactly the permitted metadata atomically."""
    _validate_scope(plan.dispositions, plan.allowed_rejected_ids)
    if not plan.changes:
        raise LegacyFilterMigrationError("The plan contains no legacy row to migrate")
    if plan.review_manifest and (
        not plan.review_manifest.is_file()
        or file_sha256(plan.review_manifest) != plan.review_manifest_sha256
    ):
        raise LegacyFilterMigrationPreconditionError("Review manifest changed after planning")
    database = resolve_database(plan.database)
    destination = (
        Path(backup_dir).expanduser().resolve()
        if backup_dir is not None else database.parent / "backups"
    )
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = destination / f"{database.stem}-legacy-filter-{timestamp}.db"
    applied_at = utc_now()
    with closing(sqlite3.connect(database, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            source_integrity = require_integrity(connection, label="source")
            require_no_table_triggers(connection, tables=("jobs", "job_scores"))
            records, results = _revalidate(plan, connection, rejector)
            before = _immutable_table_digests(connection, plan)
            backup_integrity = online_backup(database, backup)
            for change in plan.changes:
                components = _migrated_components(
                    records[change.job_id], results[change.job_id], change,
                    applied_at=applied_at, review_manifest_sha256=plan.review_manifest_sha256,
                )
                _update_row(connection, change, components, applied_at=applied_at)
            _verify_updated_rows(plan, connection, records, results, applied_at)
            if _immutable_table_digests(connection, plan) != before:
                raise LegacyFilterMigrationError("An unauthorized database value changed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise LegacyFilterMigrationError("Foreign-key verification failed")
            post_integrity = require_integrity(connection, label="updated database before commit")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return LegacyFilterMigrationReport(
        database=database, backup=backup, changed_ids=plan.change_ids,
        newly_archived_ids=tuple(change.job_id for change in plan.changes if change.archive),
        rejected_rows=sum(change.rejected for change in plan.changes),
        source_integrity=source_integrity, backup_integrity=backup_integrity,
        post_update_integrity=post_integrity, applied_at=applied_at,
    )


def _validate_scope(dispositions: tuple[str, ...], allowed_ids: frozenset[int]) -> None:
    if (
        not dispositions or len(dispositions) != len(set(dispositions))
        or not set(dispositions) <= {"uncertain", "relevant"}
    ):
        raise LegacyFilterMigrationError("Only explicit legacy uncertain/relevant scopes are supported")
    if not isinstance(allowed_ids, frozenset) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in allowed_ids
    ):
        raise LegacyFilterMigrationError("Reviewed blocker IDs must be a frozenset of positive integers")


def _decode_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LegacyFilterMigrationError("Invalid persisted JSON object") from exc
    if not isinstance(payload, dict):
        raise LegacyFilterMigrationError("Persisted JSON must be an object")
    return payload


def _load_records(
    connection: sqlite3.Connection, where: str, parameters: tuple[Any, ...],
) -> list[dict[str, Any]]:
    projections = [
        f'{alias}."{row[1]}" AS "{prefix}__{row[1]}"'
        for table, alias, prefix in (("jobs", "j", "job"), ("job_scores", "s", "score"))
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]
    projections.extend((
        "(SELECT COUNT(*) FROM applications a WHERE a.job_id = j.id) AS application_count",
        "(SELECT COUNT(*) FROM job_analyses a WHERE a.job_id = j.id) AS analysis_count",
        "(SELECT COUNT(*) FROM jobs p WHERE p.canonical_job_id = j.id "
        "OR p.possible_duplicate_of_id = j.id) AS duplicate_peer_count",
    ))
    query = (
        f"SELECT {', '.join(projections)} FROM jobs j "
        f"JOIN job_scores s ON s.job_id = j.id WHERE {where} ORDER BY j.id"
    )
    return [dict(row) for row in connection.execute(query, parameters)]


def _records_by_id(
    connection: sqlite3.Connection, ids: tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    records = []
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        records.extend(_load_records(
            connection, f"j.id IN ({','.join('?' for _ in batch)})", batch,
        ))
    return {int(record["job__id"]): record for record in records}


def _job(record: dict[str, Any]) -> LegacyJob:
    return LegacyJob(
        id=int(record["job__id"]), title=str(record["job__title"] or ""),
        company=str(record["job__company"] or ""),
        description=str(record["job__description"] or ""),
        location=record["job__location"], contract_type=record["job__contract_type"],
        remote_policy=record["job__remote_policy"], application_url=record["job__application_url"],
        source=str(record["job__source"] or ""),
        source_data=_decode_object(record["job__source_data"]) or None,
    )


def _evaluate(
    record: dict[str, Any], rejector: Rejector, allowed_ids: frozenset[int],
) -> HardRejectResult:
    from smartapply.filtering.manual_policy import (
        has_manual_filter_bypass,
        manual_filter_bypass_result,
    )

    if has_manual_filter_bypass(_decode_object(record["score__components"])):
        return manual_filter_bypass_result()
    result = rejector.evaluate(_job(record))
    if result.ruleset_version != RULESET_VERSION:
        raise LegacyFilterMigrationError(f"Unsupported ruleset: {result.ruleset_version}")
    if result.rejected and int(record["job__id"]) not in allowed_ids:
        raise LegacyFilterMigrationError(
            f"Unexpected blocker outside reviewed IDs: {record['job__id']}"
        )
    return result


def _decision_fingerprint(result: HardRejectResult) -> str:
    return fingerprint({
        "audit": result.audit_payload(), "reasons": result.reasons,
        "disposition": result.disposition.value,
    })


def _lifecycle_protections(record: dict[str, Any]) -> tuple[str, ...]:
    components = _decode_object(record["score__components"])
    protections = []
    if record["job__archived_at"] is not None or record["job__status"] == "archived":
        protections.append("existing_archive")
    if record["application_count"]:
        protections.append("application_history")
    if record["analysis_count"] or record["job__analyzed_at"] is not None:
        protections.append("analysis_history")
    if (
        record["job__canonical_job_id"] is not None
        or record["job__possible_duplicate_of_id"] is not None
        or record["duplicate_peer_count"]
        or (record["job__duplicate_review_status"] or "none") not in {"none", "rejected"}
    ):
        protections.append("duplicate_relationship")
    if (
        record["job__shortlisted_at"] is not None or record["job__shortlist_origin"] is not None
        or any(key.startswith("manual_") for key in components)
        or "reactivation" in components
        or any(
            str(reason).startswith("Manual override:")
            for reason in (components.get("reasons") or [])
        )
    ):
        protections.append("manual_or_shortlist_history")
    if components.get("rejection_stage") not in {None, "local_filter"}:
        protections.append("independent_rejection_decision")
    if record["job__status"] not in {"scraped", "filtered"}:
        protections.append("advanced_workflow_status")
    return tuple(protections)


def _revalidate(
    plan: LegacyFilterMigrationPlan, connection: sqlite3.Connection, rejector: Rejector,
) -> tuple[dict[int, dict[str, Any]], dict[int, HardRejectResult]]:
    records = _records_by_id(connection, plan.change_ids)
    if set(records) != set(plan.change_ids) or len(plan.change_ids) != len(set(plan.change_ids)):
        raise LegacyFilterMigrationPreconditionError("Planned rows disappeared or repeat")
    results = {}
    for change in plan.changes:
        record = records[change.job_id]
        if fingerprint(record) != change.row_fingerprint:
            raise LegacyFilterMigrationPreconditionError(
                f"Job {change.job_id} changed after planning"
            )
        result = _evaluate(record, rejector, plan.allowed_rejected_ids)
        protections = _lifecycle_protections(record)
        if (
            _decision_fingerprint(result) != change.decision_fingerprint
            or result.rejected != change.rejected
            or protections != change.lifecycle_protections
            or (result.rejected and not protections) != change.archive
        ):
            raise LegacyFilterMigrationPreconditionError(
                f"Decision for job {change.job_id} changed after planning"
            )
        results[change.job_id] = result
    return records, results


def _migrated_components(
    record: dict[str, Any], result: HardRejectResult, change: LegacyFilterChange,
    *, applied_at: str, review_manifest_sha256: str | None,
) -> dict[str, Any]:
    previous = _decode_object(record["score__components"])
    from smartapply.pipeline.process.score_components import LEGACY_MATCH_COMPONENT_KEYS

    components = {key: value for key, value in previous.items()
                  if key not in LEGACY_MATCH_COMPONENT_KEYS}
    preserve_independent_reason = previous.get("rejection_stage") not in {None, "local_filter"}
    if not preserve_independent_reason:
        for key in _REJECTION_KEYS:
            components.pop(key, None)
        if result.rejected:
            components.update({
                "rejection_stage": "local_filter",
                "rejection_reasons": result.blocking_reasons,
                "rejection_summary": "; ".join(result.blocking_reasons),
            })
    manual_override_reasons = [
        str(reason) for reason in (previous.get("reasons") or [])
        if str(reason).startswith("Manual override:")
    ]
    components.update({
        "reasons": list(dict.fromkeys([*result.reasons, *manual_override_reasons])),
        "filter_disposition": result.disposition.value,
        "hard_reject": result.audit_payload(),
        MIGRATION_COMPONENT: {
            "schema_version": 1, "migrated_at": applied_at,
            "ruleset_version": RULESET_VERSION,
            "review_manifest_sha256": review_manifest_sha256,
            "previous_filter_components": {
                key: value for key, value in previous.items() if key in FILTER_COMPONENT_KEYS
            },
            "lifecycle_action": "archived" if change.archive else "preserved",
            "lifecycle_protections": list(change.lifecycle_protections),
        },
    })
    return components


def _update_row(
    connection: sqlite3.Connection, change: LegacyFilterChange, components: dict[str, Any],
    *, applied_at: str,
) -> None:
    if change.archive:
        updated = connection.execute(
            "UPDATE jobs SET status = 'archived', archived_at = ? WHERE id = ?",
            (applied_at, change.job_id),
        )
        if updated.rowcount != 1:
            raise LegacyFilterMigrationError(f"Missing job {change.job_id}")
    updated = connection.execute(
        "UPDATE job_scores SET components = ? WHERE job_id = ?",
        (canonical_json(components), change.job_id),
    )
    if updated.rowcount != 1:
        raise LegacyFilterMigrationError(f"Missing score for job {change.job_id}")


def _verify_updated_rows(
    plan: LegacyFilterMigrationPlan, connection: sqlite3.Connection,
    before: dict[int, dict[str, Any]], results: dict[int, HardRejectResult], applied_at: str,
) -> None:
    after = _records_by_id(connection, plan.change_ids)
    if set(after) != set(before):
        raise LegacyFilterMigrationError("Could not reload every migrated row")
    for change in plan.changes:
        expected = _migrated_components(
            before[change.job_id], results[change.job_id], change,
            applied_at=applied_at, review_manifest_sha256=plan.review_manifest_sha256,
        )
        record = after[change.job_id]
        if _decode_object(record["score__components"]) != expected:
            raise LegacyFilterMigrationError(f"Unexpected components for job {change.job_id}")
        if change.archive and (
            record["job__status"] != "archived" or record["job__archived_at"] != applied_at
        ):
            raise LegacyFilterMigrationError(f"Unexpected archive state for job {change.job_id}")


def _immutable_table_digests(
    connection: sqlite3.Connection, plan: LegacyFilterMigrationPlan,
) -> dict[str, str]:
    """Compare all values, omitting only exactly authorized fields and rows."""
    changed = set(plan.change_ids)
    archived = {change.job_id for change in plan.changes if change.archive}
    tables = [str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )]
    digests = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = list(connection.execute(f"PRAGMA table_info({quoted})"))
        primary = sorted((row for row in columns if row[5]), key=lambda row: row[5])
        order_columns = primary or columns
        ordering = ",".join('"' + str(row[1]).replace('"', '""') + '"' for row in order_columns)
        digest = hashlib.sha256()
        digest.update(canonical_json([tuple(row) for row in columns]).encode("utf-8"))
        for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY {ordering}"):
            values = dict(row)
            if table == "job_scores" and values.get("job_id") in changed:
                values.pop("components", None)
            if table == "jobs" and values.get("id") in archived:
                values.pop("status", None)
                values.pop("archived_at", None)
            digest.update(canonical_json(values).encode("utf-8"))
            digest.update(b"\n")
        digests[table] = digest.hexdigest()
    return digests


__all__ = [
    "LegacyFilterMigrationError", "LegacyFilterMigrationPreconditionError",
    "LegacyFilterMigrationPlan", "LegacyFilterMigrationReport",
    "apply_legacy_filter_migration", "plan_legacy_filter_migration",
]
