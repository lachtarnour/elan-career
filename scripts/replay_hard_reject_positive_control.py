#!/usr/bin/env python3
"""Replay the deterministic hard rejector against the frozen positive control.

The frozen baseline defines the two control cohorts: all 1,268 rows whose old
disposition was ``relevant`` and all 2,685 rows whose old disposition was
``uncertain``.  The reviewed manifest permits hard rejection only for IDs
whose incompatibility was confirmed in the corresponding cohort.  The former
``uncertain`` cohort also keeps an aggregate ceiling, so both a new rejected ID
and a count regression fail strict mode.  Releasing an allowed rejection is
always acceptable.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartapply.filtering import (
    CandidateQualifications,
    DeterministicRejector,
    HardRejectResult,
    ruleset_from_preferences,
)
from smartapply.maintenance._sqlite_safety import (
    file_sha256,
    require_safe_output_path,
)
from smartapply.profile import get_profile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_DIR = ROOT / "artifacts" / "classification-audit-20261005"
DEFAULT_BASELINE = DEFAULT_AUDIT_DIR / "baseline.json"
DEFAULT_DATABASE = DEFAULT_AUDIT_DIR / "before.db"
DEFAULT_MANIFEST = (
    ROOT / "audit_manifests" / "classification-audit-20261005" / "positive-control-decisions.json"
)

EXPECTED_RELEVANT = 1_268
EXPECTED_UNCERTAIN = 2_685
EXPECTED_ALLOWED_REJECTED = 102
EXPECTED_FALSE_REGRESSIONS = 14
EXPECTED_AMBIGUOUS_KEEP = 18
# Updated together with the reviewed uncertain allowlist after the final
# frozen replay.  It deliberately makes a manifest-only expansion fail closed.
EXPECTED_ALLOWED_UNCERTAIN_REJECTED = 396
_CONTROL_DISPOSITIONS = ("relevant", "uncertain")
_SQLITE_PARAMETER_BATCH = 900


@dataclass(frozen=True)
class BaselineRow:
    id: int
    old_disposition: str


@dataclass(frozen=True)
class ReplayJob:
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
class PositiveControlManifest:
    allowed_rejected_ids: frozenset[int]
    false_regression_ids: frozenset[int]
    ambiguous_keep_ids: frozenset[int]
    allowed_uncertain_rejected_ids: frozenset[int]
    expected_relevant: int
    expected_uncertain: int
    max_uncertain_rejected: int
    database_sha256: str
    baseline_sha256: str

    @property
    def protected_keep_ids(self) -> frozenset[int]:
        return self.false_regression_ids | self.ambiguous_keep_ids

    @property
    def reviewed_relevant_ids(self) -> frozenset[int]:
        return self.allowed_rejected_ids | self.protected_keep_ids

    @property
    def reviewed_ids(self) -> frozenset[int]:
        return self.reviewed_relevant_ids | self.allowed_uncertain_rejected_ids


def _format_ids(ids: Iterable[int], *, limit: int = 20) -> str:
    ordered = sorted(ids)
    suffix = "" if len(ordered) <= limit else f" ... (+{len(ordered) - limit} more)"
    return f"{ordered[:limit]}{suffix}"


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected {label} to be a JSON object")
    return value


def _require_id_set(payload: dict[str, Any], key: str) -> frozenset[int]:
    values = payload.get(key)
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values
    ):
        raise RuntimeError(f"Expected {key} to be a list of positive integer IDs")
    if len(values) != len(set(values)):
        raise RuntimeError(f"Duplicate IDs in manifest field {key}")
    return frozenset(values)


def _require_sha256(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError(f"Expected a lowercase SHA-256 digest for {label}")
    return value


def _require_non_negative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"Expected {label} to be a non-negative integer")
    return value


def load_manifest(path: Path) -> PositiveControlManifest:
    """Load and fully validate the manually reviewed positive-control manifest."""

    if not path.is_file():
        raise RuntimeError(f"Positive-control manifest does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload = _require_mapping(payload, label="positive-control manifest")
    if payload.get("schema_version") != 3:
        raise RuntimeError(f"Unsupported positive-control manifest schema in {path}")

    allowed = _require_id_set(payload, "allowed_rejected_ids")
    false_regressions = _require_id_set(payload, "false_regression_ids")
    ambiguous = _require_id_set(payload, "ambiguous_keep_ids")
    allowed_uncertain = _require_id_set(payload, "allowed_uncertain_rejected_ids")
    categories = {
        "allowed_rejected_ids": allowed,
        "false_regression_ids": false_regressions,
        "ambiguous_keep_ids": ambiguous,
    }
    for name, ids in categories.items():
        overlap = ids & set().union(
            *(other for other_name, other in categories.items() if other_name != name)
        )
        if overlap:
            raise RuntimeError(
                f"Manifest decision categories overlap in {name}: {_format_ids(overlap)}"
            )
    reviewed_relevant = allowed | false_regressions | ambiguous
    cross_cohort_overlap = reviewed_relevant & allowed_uncertain
    if cross_cohort_overlap:
        raise RuntimeError(
            "Manifest relevant and uncertain decisions overlap: "
            f"{_format_ids(cross_cohort_overlap)}"
        )

    expected_counts = _require_mapping(
        payload.get("expected_counts"), label="manifest expected_counts"
    )
    max_uncertain_rejected = _require_non_negative_int(
        expected_counts.get("max_uncertain_rejected"),
        label="manifest expected_counts.max_uncertain_rejected",
    )
    if max_uncertain_rejected > EXPECTED_UNCERTAIN:
        raise RuntimeError(
            "Manifest uncertain rejection ceiling exceeds the frozen uncertain cohort: "
            f"{max_uncertain_rejected} > {EXPECTED_UNCERTAIN}"
        )
    exact_counts = {
        "baseline_relevant": EXPECTED_RELEVANT,
        "baseline_uncertain": EXPECTED_UNCERTAIN,
        "reviewed_relevant_rejected": (
            EXPECTED_ALLOWED_REJECTED + EXPECTED_FALSE_REGRESSIONS + EXPECTED_AMBIGUOUS_KEEP
        ),
        "allowed_rejected": EXPECTED_ALLOWED_REJECTED,
        "false_regression": EXPECTED_FALSE_REGRESSIONS,
        "ambiguous_keep": EXPECTED_AMBIGUOUS_KEEP,
        "allowed_uncertain_rejected": EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
    }
    for key, expected in exact_counts.items():
        if expected_counts.get(key) != expected:
            raise RuntimeError(
                f"Manifest expected count {key!r} is {expected_counts.get(key)!r}; "
                f"expected {expected}"
            )
    actual_counts = {
        "allowed_rejected": len(allowed),
        "false_regression": len(false_regressions),
        "ambiguous_keep": len(ambiguous),
        "allowed_uncertain_rejected": len(allowed_uncertain),
    }
    for key, actual in actual_counts.items():
        if actual != exact_counts[key]:
            raise RuntimeError(
                f"Manifest field {key!r} contains {actual} IDs; expected {exact_counts[key]}"
            )
    if max_uncertain_rejected > len(allowed_uncertain):
        raise RuntimeError(
            "Manifest uncertain rejection ceiling exceeds its reviewed ID allowlist: "
            f"{max_uncertain_rejected} > {len(allowed_uncertain)}"
        )
    if len(allowed | false_regressions | ambiguous) != exact_counts["reviewed_relevant_rejected"]:
        raise RuntimeError(
            "Manifest decisions do not cover exactly "
            f"{exact_counts['reviewed_relevant_rejected']} reviewed IDs"
        )

    provenance = _require_mapping(payload.get("provenance"), label="manifest provenance")
    database = _require_mapping(provenance.get("database"), label="database provenance")
    baseline = _require_mapping(provenance.get("baseline"), label="baseline provenance")
    uncertain_control = _require_mapping(
        provenance.get("uncertain_rejection_control"),
        label="uncertain rejection control provenance",
    )
    if uncertain_control.get("kind") != "id_allowlist_and_aggregate_count_ceiling":
        raise RuntimeError(
            "Manifest uncertain rejection control must use "
            "id_allowlist_and_aggregate_count_ceiling"
        )
    if uncertain_control.get("id_level_review") is not True:
        raise RuntimeError(
            "Manifest uncertain rejection control must state that ID-level review exists"
        )
    if uncertain_control.get("allowed_rejected") != len(allowed_uncertain):
        raise RuntimeError(
            "Manifest uncertain rejection allowlist count disagrees with its provenance"
        )
    if uncertain_control.get("maximum_rejected") != max_uncertain_rejected:
        raise RuntimeError(
            "Manifest uncertain rejection ceiling disagrees with its provenance"
        )
    for key in ("basis", "method", "status"):
        value = uncertain_control.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"Manifest uncertain rejection control requires non-empty {key!r} provenance"
            )
    return PositiveControlManifest(
        allowed_rejected_ids=allowed,
        false_regression_ids=false_regressions,
        ambiguous_keep_ids=ambiguous,
        allowed_uncertain_rejected_ids=allowed_uncertain,
        expected_relevant=EXPECTED_RELEVANT,
        expected_uncertain=EXPECTED_UNCERTAIN,
        max_uncertain_rejected=max_uncertain_rejected,
        database_sha256=_require_sha256(database.get("sha256"), label="database"),
        baseline_sha256=_require_sha256(baseline.get("sha256"), label="baseline"),
    )


def _load_baseline(
    path: Path,
    *,
    expected_relevant: int,
    expected_uncertain: int,
) -> tuple[dict[int, BaselineRow], dict[str, int], int]:
    if not path.is_file():
        raise RuntimeError(f"Frozen baseline does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise RuntimeError(f"Expected a JSON row list in {path}")

    all_ids: set[int] = set()
    selected: dict[int, BaselineRow] = {}
    disposition_counts: Counter[str] = Counter()
    for index, row in enumerate(payload):
        job_id = row.get("id")
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise RuntimeError(f"Invalid baseline ID at row {index}: {job_id!r}")
        if job_id in all_ids:
            raise RuntimeError(f"Duplicate baseline ID {job_id}")
        all_ids.add(job_id)
        disposition = row.get("old_disposition")
        disposition_key = str(disposition) if disposition is not None else "null"
        disposition_counts[disposition_key] += 1
        if disposition in _CONTROL_DISPOSITIONS:
            selected[job_id] = BaselineRow(id=job_id, old_disposition=disposition)

    actual_counts = Counter(row.old_disposition for row in selected.values())
    expected_counts = {
        "relevant": expected_relevant,
        "uncertain": expected_uncertain,
    }
    if actual_counts != expected_counts:
        raise RuntimeError(
            "Frozen positive-control cohort mismatch: "
            f"found {dict(actual_counts)}, expected {expected_counts}"
        )
    return selected, dict(sorted(disposition_counts.items())), len(payload)


def _decode_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) or value is None:
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _batches(ids: Iterable[int]) -> Iterable[list[int]]:
    ordered = sorted(ids)
    for start in range(0, len(ordered), _SQLITE_PARAMETER_BATCH):
        yield ordered[start : start + _SQLITE_PARAMETER_BATCH]


def _load_jobs(database: Path, ids: set[int]) -> dict[int, ReplayJob]:
    """Read frozen jobs without allowing SQLite recovery or writes."""

    if not database.is_file():
        raise RuntimeError(f"Frozen database does not exist: {database}")
    if not ids:
        return {}

    # Immutable SQLite deliberately ignores recovery journals, so reject any
    # non-empty sidecar rather than silently replaying an incomplete snapshot.
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.exists() and sidecar.stat().st_size:
            raise RuntimeError(
                "Frozen database has a non-empty SQLite sidecar and is not "
                f"self-contained: {sidecar}"
            )

    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    rows: list[sqlite3.Row] = []
    try:
        connection.execute("PRAGMA query_only = ON")
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("Frozen database integrity check failed") from exc
        if integrity.lower() != "ok":
            raise RuntimeError(f"Frozen database integrity check failed: {integrity}")

        for batch in _batches(ids):
            placeholders = ",".join("?" for _ in batch)
            query = f"""
                SELECT id, title, company, description, location, contract_type,
                       remote_policy, application_url, source, source_data
                FROM jobs
                WHERE id IN ({placeholders})
                ORDER BY id
            """
            rows.extend(connection.execute(query, batch).fetchall())
    finally:
        connection.close()

    jobs: dict[int, ReplayJob] = {}
    for row in rows:
        job_id = int(row["id"])
        if job_id in jobs:
            raise RuntimeError(f"Frozen database returned duplicate job ID {job_id}")
        jobs[job_id] = ReplayJob(
            id=job_id,
            title=row["title"] or "",
            company=row["company"] or "",
            description=row["description"] or "",
            location=row["location"],
            contract_type=row["contract_type"],
            remote_policy=row["remote_policy"],
            application_url=row["application_url"],
            source=row["source"] or "",
            source_data=_decode_json_object(row["source_data"]),
        )
    return jobs


def _strict_findings(
    *,
    relevant_ids: set[int],
    uncertain_ids: set[int],
    results: dict[int, HardRejectResult],
    manifest: PositiveControlManifest,
) -> dict[str, Any]:
    rejected = {job_id for job_id, result in results.items() if result.rejected}
    relevant_rejected = rejected & relevant_ids
    uncertain_rejected = rejected & uncertain_ids
    unexpected_relevant = relevant_rejected - manifest.allowed_rejected_ids
    protected_rejected = relevant_rejected & manifest.protected_keep_ids
    unexpected_uncertain = (
        uncertain_rejected - manifest.allowed_uncertain_rejected_ids
    )
    violations = unexpected_relevant | protected_rejected | unexpected_uncertain
    uncertain_rejected_count = len(uncertain_rejected)
    uncertain_ceiling_exceeded = (
        uncertain_rejected_count > manifest.max_uncertain_rejected
    )
    return {
        "passed": not violations and not uncertain_ceiling_exceeded,
        "unexpected_relevant_rejected_ids": sorted(unexpected_relevant),
        "protected_keep_rejected_ids": sorted(protected_rejected),
        "unexpected_uncertain_rejected_ids": sorted(unexpected_uncertain),
        "violation_ids": sorted(violations),
        "relevant_rejected_ids": sorted(relevant_rejected),
        "uncertain_rejected_ids": sorted(uncertain_rejected),
        "uncertain_rejected_count": uncertain_rejected_count,
        "max_uncertain_rejected": manifest.max_uncertain_rejected,
        "uncertain_rejection_ceiling_exceeded": uncertain_ceiling_exceeded,
        "uncertain_rejection_overage": max(
            0,
            uncertain_rejected_count - manifest.max_uncertain_rejected,
        ),
    }


def _cohort_report(
    ids: set[int],
    results: dict[int, HardRejectResult],
) -> dict[str, Any]:
    rejected_ids: list[int] = []
    gate_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    for job_id in sorted(ids):
        result = results[job_id]
        if not result.rejected:
            continue
        rejected_ids.append(job_id)
        for decision in result.decisions:
            if not decision.blocked:
                continue
            gate_counts[decision.gate] += 1
            reason_counts.update(decision.reason_codes)
    return {
        "evaluated": len(ids),
        "rejected": len(rejected_ids),
        "pending_relevance": len(ids) - len(rejected_ids),
        "rejected_ids": rejected_ids,
        "blocked_gate_counts_overlap": dict(sorted(gate_counts.items())),
        "blocking_reason_counts_overlap": dict(sorted(reason_counts.items())),
    }


def _rejected_records(
    *,
    baseline: dict[int, BaselineRow],
    jobs: dict[int, ReplayJob],
    results: dict[int, HardRejectResult],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for job_id in sorted(results):
        result = results[job_id]
        if not result.rejected:
            continue
        blocked = [decision for decision in result.decisions if decision.blocked]
        records.append(
            {
                "id": job_id,
                "title": jobs[job_id].title,
                "company": jobs[job_id].company,
                "old_disposition": baseline[job_id].old_disposition,
                "blocked_gates": [decision.gate for decision in blocked],
                "blocking_reasons": [
                    reason for decision in blocked for reason in decision.reason_codes
                ],
            }
        )
    return records


def replay(
    baseline_path: Path,
    database_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    actual_baseline_sha256 = file_sha256(baseline_path)
    if actual_baseline_sha256 != manifest.baseline_sha256:
        raise RuntimeError(
            "Frozen baseline hash mismatch: "
            f"expected {manifest.baseline_sha256}, found {actual_baseline_sha256}"
        )
    actual_database_sha256 = file_sha256(database_path)
    if actual_database_sha256 != manifest.database_sha256:
        raise RuntimeError(
            "Frozen database hash mismatch: "
            f"expected {manifest.database_sha256}, found {actual_database_sha256}"
        )

    baseline, baseline_disposition_counts, baseline_rows = _load_baseline(
        baseline_path,
        expected_relevant=manifest.expected_relevant,
        expected_uncertain=manifest.expected_uncertain,
    )
    relevant_ids = {job_id for job_id, row in baseline.items() if row.old_disposition == "relevant"}
    uncertain_ids = set(baseline) - relevant_ids
    if not manifest.reviewed_relevant_ids <= relevant_ids:
        missing = manifest.reviewed_relevant_ids - relevant_ids
        raise RuntimeError(
            "Reviewed positive-control IDs are absent from the relevant baseline cohort: "
            f"{_format_ids(missing)}"
        )
    if not manifest.allowed_uncertain_rejected_ids <= uncertain_ids:
        missing = manifest.allowed_uncertain_rejected_ids - uncertain_ids
        raise RuntimeError(
            "Reviewed uncertain rejection IDs are absent from the uncertain baseline cohort: "
            f"{_format_ids(missing)}"
        )

    jobs = _load_jobs(database_path, set(baseline))
    missing_jobs = set(baseline) - jobs.keys()
    extra_jobs = jobs.keys() - set(baseline)
    if missing_jobs or extra_jobs:
        raise RuntimeError(
            "Frozen database control coverage mismatch: "
            f"missing={_format_ids(missing_jobs)}, extra={_format_ids(extra_jobs)}"
        )

    profile = get_profile()
    rejector = DeterministicRejector(
        ruleset_from_preferences(profile.preferences),
        qualifications=CandidateQualifications.from_profile(profile),
    )
    results = {job_id: rejector.evaluate(jobs[job_id]) for job_id in sorted(jobs)}
    ruleset_versions = {result.ruleset_version for result in results.values()}
    if len(ruleset_versions) != 1:
        raise RuntimeError(f"Replay produced mixed ruleset versions: {ruleset_versions}")

    findings = _strict_findings(
        relevant_ids=relevant_ids,
        uncertain_ids=uncertain_ids,
        results=results,
        manifest=manifest,
    )
    allowed_still_rejected = manifest.allowed_rejected_ids & set(findings["relevant_rejected_ids"])
    return {
        "schema_version": 3,
        "ruleset_version": next(iter(ruleset_versions)),
        "inputs": {
            "baseline": {
                "path": str(baseline_path.resolve()),
                "sha256": actual_baseline_sha256,
                "rows": baseline_rows,
                "old_disposition_counts": baseline_disposition_counts,
            },
            "database": {
                "path": str(database_path.resolve()),
                "sha256": actual_database_sha256,
                "opened": "SQLite URI mode=ro&immutable=1; PRAGMA query_only=ON",
                "integrity_check": "ok",
            },
            "manifest": {
                "path": str(manifest_path.resolve()),
                "sha256": file_sha256(manifest_path),
            },
        },
        "manifest_decisions": {
            "allowed_rejected_ids": sorted(manifest.allowed_rejected_ids),
            "allowed_rejected_still_rejected_ids": sorted(allowed_still_rejected),
            "allowed_rejected_released_ids": sorted(
                manifest.allowed_rejected_ids - allowed_still_rejected
            ),
            "false_regression_ids": sorted(manifest.false_regression_ids),
            "ambiguous_keep_ids": sorted(manifest.ambiguous_keep_ids),
            "protected_keep_ids": sorted(manifest.protected_keep_ids),
            "uncertain_rejection_control": {
                "kind": "id_allowlist_and_aggregate_count_ceiling",
                "id_level_review": True,
                "allowed_rejected_ids": sorted(
                    manifest.allowed_uncertain_rejected_ids
                ),
                "allowed_rejected_still_rejected_ids": sorted(
                    manifest.allowed_uncertain_rejected_ids
                    & set(findings["uncertain_rejected_ids"])
                ),
                "allowed_rejected_released_ids": sorted(
                    manifest.allowed_uncertain_rejected_ids
                    - set(findings["uncertain_rejected_ids"])
                ),
                "maximum_rejected": manifest.max_uncertain_rejected,
            },
        },
        "relevant": _cohort_report(relevant_ids, results),
        "uncertain": _cohort_report(uncertain_ids, results),
        "strict": {
            "passed": findings["passed"],
            "unexpected_relevant_rejected_ids": findings["unexpected_relevant_rejected_ids"],
            "protected_keep_rejected_ids": findings["protected_keep_rejected_ids"],
            "unexpected_uncertain_rejected_ids": findings[
                "unexpected_uncertain_rejected_ids"
            ],
            "violation_ids": findings["violation_ids"],
            "uncertain_rejected_count": findings["uncertain_rejected_count"],
            "max_uncertain_rejected": findings["max_uncertain_rejected"],
            "uncertain_rejection_ceiling_exceeded": findings[
                "uncertain_rejection_ceiling_exceeded"
            ],
            "uncertain_rejection_overage": findings["uncertain_rejection_overage"],
        },
        "rejected_records": _rejected_records(
            baseline=baseline,
            jobs=jobs,
            results=results,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit 1 if a relevant row outside allowed_rejected_ids is rejected, "
            "including any reviewed protected keep, if a former uncertain row outside "
            "allowed_uncertain_rejected_ids is rejected, or if rejected former "
            "uncertain rows exceed the manifest ceiling. Releasing an allowed "
            "rejection never fails."
        ),
    )
    args = parser.parse_args()
    output = require_safe_output_path(
        args.output,
        protected_paths=(args.baseline, args.manifest),
        sqlite_databases=(args.database,),
    )
    summary = replay(args.baseline, args.database, args.manifest)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if args.strict and not summary["strict"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
