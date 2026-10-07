#!/usr/bin/env python3
"""Replay hard-reject-v2 against the frozen exhaustive rejection audit.

The database is opened with SQLite's read-only mode. All 6,763 audited IDs are
verified against the frozen snapshot, while the 808 records whose audit is only
about duplicate identity are deliberately excluded from semantic gates.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from smartapply.filtering import (
    CandidateQualifications,
    DeterministicRejector,
    HardRejectResult,
    ruleset_from_preferences,
)
from smartapply.filtering.hard_reject.input_fingerprint import (
    hard_reject_input_sha256,
)
from smartapply.maintenance._sqlite_safety import require_safe_output_path
from smartapply.profile import get_profile

EXPECTED_AUDITED_IDS = 6_763
EXPECTED_IDENTITY_ONLY_IDS = 808
_SQLITE_PARAMETER_BATCH = 900
DEFAULT_REASON_CORRECTIONS = (
    Path(__file__).resolve().parents[1]
    / "audit_manifests"
    / "classification-audit-20261005"
    / "reason-corrections.json"
)
_KNOWN_GATE_NAMES = frozenset(
    {"language", "contract", "experience", "qualification", "seniority", "role"}
)

_SEMANTIC_AUDIT_FILES = (
    "exhaustive-role.json",
    "exhaustive-experience.json",
    "exhaustive-contract.json",
    "exhaustive-score.json",
)
_SEMANTIC_DECISIONS = {
    "valid_rejection": "protected_rejection",
    "certain_false_rejection": "certain_false_rejection",
    "probable_false_rejection": "probable_false_rejection",
    "ambiguous": "ambiguous",
}
_IDENTITY_ONLY_DECISIONS = {
    "ambiguous_duplicate_identity",
    "not_a_distinct_lost_opportunity",
}


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
    persisted_filter_disposition: str | None


@dataclass(frozen=True)
class AuditExpectation:
    id: int
    audit_file: str
    audit_decision: str
    cohort: str

    @property
    def semantically_applicable(self) -> bool:
        return self.cohort != "identity_only"


@dataclass(frozen=True)
class AuditReasonCorrection:
    """Reviewed correction to one gate reason in the frozen audit."""

    id: int
    gate: str
    explanation: str


@dataclass(frozen=True)
class OldRejection:
    id: int
    bucket: str
    stage: str
    reason: str
    reasons: tuple[str, ...]


@dataclass
class ReplayDiff:
    id: int
    title: str
    company: str
    audited_input_sha256: str
    audit_file: str
    audit_decision: str
    classification: str
    classification_reason: str
    old_disposition: str
    old_bucket: str
    old_stage: str
    old_reason: str
    old_reasons: list[str]
    new_disposition: str
    new_rejected: bool
    new_context_reasons: list[str]
    new_gates: list[dict[str, Any]]


@dataclass
class ReplaySummary:
    ruleset_version: str
    audited_ids: int
    database_verified_ids: int
    persisted_rejected_ids: int
    semantic_gate_ids: int
    semantic_evaluated_ids: int
    semantic_rejected_ids: int
    semantic_kept_ids: int
    duplicate_identity_only_ids: int
    duplicate_identity_only_reason_counts: dict[str, int]
    protected_rejections: int
    protected_still_rejected: int
    protected_released_ids: list[int]
    certain_false_rejections: int
    certain_rescued: int
    certain_still_rejected_ids: list[int]
    probable_false_rejections: int
    probable_rescued: int
    probable_still_rejected_ids: list[int]
    ambiguous_semantic: int
    ambiguous_kept: int
    ambiguous_rejected_ids: list[int]
    policy_correction_exception_ids: list[int]
    policy_correction_applied_ids: list[int]
    policy_correction_still_rejected_ids: list[int]
    policy_correction_failed_ids: list[int]
    true_regression_count: int
    true_regression_ids: list[int]
    diff_record_count: int
    diff_records: list[ReplayDiff]


def _rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError(f"Expected a JSON row list in {path}")
    return rows


def _format_ids(ids: Iterable[int], *, limit: int = 20) -> str:
    ordered = sorted(ids)
    suffix = "" if len(ordered) <= limit else f" ... (+{len(ordered) - limit} more)"
    return f"{ordered[:limit]}{suffix}"


def _register_expectation(
    expectations: dict[int, AuditExpectation],
    *,
    row: dict[str, Any],
    audit_file: str,
    audit_decision: str,
    cohort: str,
) -> None:
    job_id = int(row["id"])
    if job_id in expectations:
        previous = expectations[job_id]
        raise RuntimeError(
            f"Audited ID {job_id} occurs in both {previous.audit_file} and {audit_file}"
        )
    expectations[job_id] = AuditExpectation(
        id=job_id,
        audit_file=audit_file,
        audit_decision=audit_decision,
        cohort=cohort,
    )


def _load_expectations(
    audit_dir: Path,
) -> tuple[dict[int, AuditExpectation], Counter[str]]:
    expectations: dict[int, AuditExpectation] = {}
    for filename in _SEMANTIC_AUDIT_FILES:
        for row in _rows(audit_dir / filename):
            decision = str(row.get("decision", ""))
            try:
                cohort = _SEMANTIC_DECISIONS[decision]
            except KeyError as exc:
                raise RuntimeError(
                    f"Unknown semantic audit decision {decision!r} in {filename}"
                ) from exc
            _register_expectation(
                expectations,
                row=row,
                audit_file=filename,
                audit_decision=decision,
                cohort=cohort,
            )

    identity_reason_counts: Counter[str] = Counter()
    filename = "exhaustive-duplicates.json"
    for row in _rows(audit_dir / filename):
        eligibility = str(row.get("eligibility_after_link_removal", ""))
        if eligibility == "valid_rejection_other_gate":
            cohort = "protected_rejection"
        elif eligibility == "certain_false_rejection":
            cohort = "certain_false_rejection"
        elif eligibility in _IDENTITY_ONLY_DECISIONS:
            cohort = "identity_only"
            identity_reason_counts[eligibility] += 1
        else:
            raise RuntimeError(
                f"Unknown duplicate eligibility {eligibility!r} for ID {row.get('id')}"
            )
        _register_expectation(
            expectations,
            row=row,
            audit_file=filename,
            audit_decision=eligibility,
            cohort=cohort,
        )

    summary = json.loads((audit_dir / "exhaustive-summary.json").read_text(encoding="utf-8"))
    expected_total = int(summary["covered_total"])
    if expected_total != EXPECTED_AUDITED_IDS:
        raise RuntimeError(
            f"Frozen audit declares {expected_total} rows; expected {EXPECTED_AUDITED_IDS}"
        )
    if len(expectations) != expected_total:
        raise RuntimeError(
            f"Audit coverage mismatch: expected {expected_total}, found "
            f"{len(expectations)} unique IDs"
        )

    identity_only_ids = {
        job_id
        for job_id, expectation in expectations.items()
        if not expectation.semantically_applicable
    }
    if len(identity_only_ids) != EXPECTED_IDENTITY_ONLY_IDS:
        raise RuntimeError(
            "Identity-only coverage mismatch: expected "
            f"{EXPECTED_IDENTITY_ONLY_IDS}, found {len(identity_only_ids)}"
        )

    return expectations, identity_reason_counts


def _load_reason_corrections(
    path: Path,
    expectations: dict[int, AuditExpectation],
) -> dict[int, AuditReasonCorrection]:
    """Load gate-specific audit corrections kept outside production rules."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError(f"Unsupported reason-correction schema in {path}")
    rows = payload.get("corrections")
    if not isinstance(rows, list):
        raise RuntimeError(f"Expected a correction list in {path}")

    corrections: dict[int, AuditReasonCorrection] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError(f"Expected correction objects in {path}")
        try:
            job_id = int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid correction ID in {path}") from exc
        gate = row.get("gate")
        explanation = row.get("explanation")
        if not isinstance(gate, str) or gate not in _KNOWN_GATE_NAMES:
            raise RuntimeError(f"Invalid correction gate for ID {job_id}: {gate!r}")
        if not isinstance(explanation, str) or not explanation.strip():
            raise RuntimeError(f"Missing correction explanation for ID {job_id}")
        if job_id in corrections:
            raise RuntimeError(f"Duplicate reason correction for ID {job_id}")
        expectation = expectations.get(job_id)
        if expectation is None or expectation.cohort != "protected_rejection":
            raise RuntimeError(
                f"Reason correction {job_id} is absent or not a protected audit row"
            )
        corrections[job_id] = AuditReasonCorrection(
            id=job_id,
            gate=gate,
            explanation=explanation.strip(),
        )
    return corrections


def _load_old_rejections(
    audit_dir: Path,
    expected_ids: set[int],
) -> dict[int, OldRejection]:
    old_rejections: dict[int, OldRejection] = {}
    for row in _rows(audit_dir / "agent-review-rejected-rows.json"):
        job_id = int(row["id"])
        if job_id in old_rejections:
            raise RuntimeError(f"Duplicate old rejection record for ID {job_id}")
        reasons = row.get("decisive_reasons")
        if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
            raise RuntimeError(f"Invalid decisive_reasons for ID {job_id}")
        old_rejections[job_id] = OldRejection(
            id=job_id,
            bucket=str(row["bucket"]),
            stage=str(row["stage"]),
            reason=str(row["decisive_reason"]),
            reasons=tuple(reasons),
        )

    actual_ids = set(old_rejections)
    if actual_ids != expected_ids:
        missing = expected_ids - actual_ids
        extra = actual_ids - expected_ids
        raise RuntimeError(
            "Old-rejection partition does not exactly match the exhaustive audit; "
            f"missing={_format_ids(missing)}, extra={_format_ids(extra)}"
        )
    return old_rejections


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
    if not database.is_file():
        raise RuntimeError(f"Frozen database does not exist: {database}")
    if not ids:
        return {}

    # Immutable mode prevents SQLite from recovering either journaling mode.
    # The frozen database must therefore be self-contained before it can be an
    # authority for recovery decisions.
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
            raise RuntimeError(
                f"Frozen database integrity check failed: {integrity}"
            )
        for batch in _batches(ids):
            placeholders = ",".join("?" for _ in batch)
            query = f"""
                SELECT j.id, j.title, j.company, j.description, j.location,
                       j.contract_type, j.remote_policy, j.application_url,
                       j.source, j.source_data, s.components AS score_components
                FROM jobs AS j
                LEFT JOIN job_scores AS s ON s.job_id = j.id
                WHERE j.id IN ({placeholders})
                ORDER BY j.id
            """
            rows.extend(connection.execute(query, batch).fetchall())
    finally:
        connection.close()

    jobs: dict[int, ReplayJob] = {}
    for row in rows:
        job_id = int(row["id"])
        if job_id in jobs:
            raise RuntimeError(f"Frozen database returned duplicate job ID {job_id}")
        score_components = _decode_json_object(row["score_components"])
        disposition = score_components.get("filter_disposition") if score_components else None
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
            persisted_filter_disposition=(str(disposition) if disposition is not None else None),
        )
    return jobs


def _finding_classification(
    *,
    cohort: str,
    rejected: bool,
    correction: AuditReasonCorrection | None = None,
    corrected_gate_blocked: bool = False,
) -> tuple[str, str] | None:
    if correction is not None:
        if corrected_gate_blocked:
            return (
                "policy_correction_regression",
                f"{correction.explanation} The {correction.gate} gate still blocks it.",
            )
        if rejected:
            return (
                "policy_correction_satisfied_other_gate_rejected",
                f"{correction.explanation} The corrected {correction.gate} gate now passes, "
                "but another deterministic gate rejects the offer.",
            )
        return "policy_correction_exception", correction.explanation
    if cohort == "protected_rejection" and not rejected:
        return (
            "protected_release_pending_relevance",
            "The frozen audit found the offer off-target, but the new hard-reject "
            "stage has no decisive blocker. It is handed to relevance classification.",
        )
    if cohort == "certain_false_rejection":
        if rejected:
            return (
                "true_regression",
                "A confirmed false rejection remains blocked by the new gates.",
            )
        return (
            "confirmed_false_rejection_rescued",
            "The new gates release a confirmed false rejection from the frozen audit.",
        )
    if cohort == "probable_false_rejection":
        if rejected:
            return (
                "probable_false_rejection_still_rejected",
                "A probable false rejection remains blocked; this is review evidence, not a strict regression.",
            )
        return (
            "probable_false_rejection_rescued",
            "The new gates release a probable false rejection.",
        )
    if cohort == "ambiguous" and not rejected:
        return (
            "ambiguous_released",
            "The new gates release a row whose frozen semantic audit remained ambiguous.",
        )
    return None


def _make_diff(
    *,
    job: ReplayJob,
    expectation: AuditExpectation,
    old_rejection: OldRejection,
    result: HardRejectResult,
    classification: tuple[str, str],
) -> ReplayDiff:
    label, reason = classification
    return ReplayDiff(
        id=job.id,
        title=job.title,
        company=job.company,
        audited_input_sha256=hard_reject_input_sha256(job),
        audit_file=expectation.audit_file,
        audit_decision=expectation.audit_decision,
        classification=label,
        classification_reason=reason,
        old_disposition="rejected",
        old_bucket=old_rejection.bucket,
        old_stage=old_rejection.stage,
        old_reason=old_rejection.reason,
        old_reasons=list(old_rejection.reasons),
        new_disposition=result.disposition.value,
        new_rejected=result.rejected,
        new_context_reasons=list(result.context_reasons),
        new_gates=[decision.to_dict() for decision in result.decisions],
    )


def replay(
    audit_dir: Path,
    database: Path,
    reason_corrections: Path = DEFAULT_REASON_CORRECTIONS,
) -> ReplaySummary:
    expectations, identity_reason_counts = _load_expectations(audit_dir)
    corrections = _load_reason_corrections(reason_corrections, expectations)
    all_ids = set(expectations)
    old_rejections = _load_old_rejections(audit_dir, all_ids)

    # Load every audited job, including duplicate-identity-only records. This
    # is a coverage verification; only the semantic subset is evaluated below.
    jobs = _load_jobs(database, all_ids)
    missing = all_ids - jobs.keys()
    if missing:
        raise RuntimeError(f"Frozen database is missing audited IDs: {_format_ids(missing)}")
    non_rejected = {
        job_id: jobs[job_id].persisted_filter_disposition
        for job_id in sorted(all_ids)
        if jobs[job_id].persisted_filter_disposition != "rejected"
    }
    if non_rejected:
        preview = list(non_rejected.items())[:20]
        raise RuntimeError(f"Frozen database no longer marks every audited ID rejected: {preview}")

    cohort_ids = {
        cohort: {
            job_id for job_id, expectation in expectations.items() if expectation.cohort == cohort
        }
        for cohort in (
            "protected_rejection",
            "certain_false_rejection",
            "probable_false_rejection",
            "ambiguous",
            "identity_only",
        )
    }
    semantic_ids = all_ids - cohort_ids["identity_only"]
    if len(semantic_ids) + len(cohort_ids["identity_only"]) != len(all_ids):
        raise RuntimeError("Semantic and identity-only audit cohorts do not partition all IDs")

    profile = get_profile()
    rejector = DeterministicRejector(
        ruleset_from_preferences(profile.preferences),
        qualifications=CandidateQualifications.from_profile(profile),
    )
    results = {job_id: rejector.evaluate(jobs[job_id]) for job_id in sorted(semantic_ids)}
    rejected_ids = {job_id for job_id, result in results.items() if result.rejected}
    ruleset_versions = {result.ruleset_version for result in results.values()}
    if len(ruleset_versions) != 1:
        raise RuntimeError(f"Replay produced mixed ruleset versions: {ruleset_versions}")
    ruleset_version = next(iter(ruleset_versions))

    protected = cohort_ids["protected_rejection"]
    certain = cohort_ids["certain_false_rejection"]
    probable = cohort_ids["probable_false_rejection"]
    ambiguous = cohort_ids["ambiguous"]
    protected_released = protected - rejected_ids
    certain_still_rejected = certain & rejected_ids
    policy_correction_ids = set(corrections)
    corrected_gate_blocked_ids: set[int] = set()
    for job_id, correction in corrections.items():
        matching_decisions = [
            decision for decision in results[job_id].decisions if decision.gate == correction.gate
        ]
        if len(matching_decisions) != 1:
            raise RuntimeError(
                f"Expected one {correction.gate!r} gate for correction {job_id}; "
                f"found {len(matching_decisions)}"
            )
        if matching_decisions[0].blocked:
            corrected_gate_blocked_ids.add(job_id)
    policy_correction_applied_ids = policy_correction_ids - corrected_gate_blocked_ids
    policy_correction_still_rejected_ids = policy_correction_ids & rejected_ids
    # A previously valid semantic rejection is not automatically a hard-gate
    # regression: the new stage deliberately refuses to decide relevance.
    # Those releases stay fully visible above.  Strict failures are reserved
    # for offers the frozen audit proved were false rejections and that remain
    # blocked by a deterministic gate.
    true_regression_ids = certain_still_rejected | corrected_gate_blocked_ids

    diff_records: list[ReplayDiff] = []
    for job_id in sorted(semantic_ids):
        result = results[job_id]
        expectation = expectations[job_id]
        correction = corrections.get(job_id)
        classification = _finding_classification(
            cohort=expectation.cohort,
            rejected=result.rejected,
            correction=correction,
            corrected_gate_blocked=job_id in corrected_gate_blocked_ids,
        )
        if classification is None:
            continue
        diff_records.append(
            _make_diff(
                job=jobs[job_id],
                expectation=expectation,
                old_rejection=old_rejections[job_id],
                result=result,
                classification=classification,
            )
        )

    return ReplaySummary(
        ruleset_version=ruleset_version,
        audited_ids=len(all_ids),
        database_verified_ids=len(jobs),
        persisted_rejected_ids=len(all_ids),
        semantic_gate_ids=len(semantic_ids),
        semantic_evaluated_ids=len(results),
        semantic_rejected_ids=len(rejected_ids),
        semantic_kept_ids=len(semantic_ids - rejected_ids),
        duplicate_identity_only_ids=len(cohort_ids["identity_only"]),
        duplicate_identity_only_reason_counts=dict(sorted(identity_reason_counts.items())),
        protected_rejections=len(protected),
        protected_still_rejected=len(protected & rejected_ids),
        protected_released_ids=sorted(protected_released),
        certain_false_rejections=len(certain),
        certain_rescued=len(certain - rejected_ids),
        certain_still_rejected_ids=sorted(certain_still_rejected),
        probable_false_rejections=len(probable),
        probable_rescued=len(probable - rejected_ids),
        probable_still_rejected_ids=sorted(probable & rejected_ids),
        ambiguous_semantic=len(ambiguous),
        ambiguous_kept=len(ambiguous - rejected_ids),
        ambiguous_rejected_ids=sorted(ambiguous & rejected_ids),
        policy_correction_exception_ids=sorted(policy_correction_ids),
        policy_correction_applied_ids=sorted(policy_correction_applied_ids),
        policy_correction_still_rejected_ids=sorted(policy_correction_still_rejected_ids),
        policy_correction_failed_ids=sorted(corrected_gate_blocked_ids),
        true_regression_count=len(true_regression_ids),
        true_regression_ids=sorted(true_regression_ids),
        diff_record_count=len(diff_records),
        diff_records=diff_records,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=Path("artifacts/classification-audit-20261005"),
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--reason-corrections",
        type=Path,
        default=DEFAULT_REASON_CORRECTIONS,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail when a confirmed false rejection remains blocked or a reviewed "
            "gate-reason correction regresses. Protected offers released to pending "
            "relevance remain visible in the report."
        ),
    )
    args = parser.parse_args()
    database = args.database or args.audit_dir / "before.db"
    protected_audit_files = tuple(
        path for path in args.audit_dir.rglob("*") if path.is_file()
    )
    output = require_safe_output_path(
        args.output,
        protected_paths=(*protected_audit_files, args.reason_corrections),
        sqlite_databases=(database,),
    )
    summary = replay(args.audit_dir, database, args.reason_corrections)
    rendered = json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if args.strict and summary.true_regression_count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
