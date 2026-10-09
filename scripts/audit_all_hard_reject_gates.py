#!/usr/bin/env python3
"""Read-only replay of every stored offer through the six current local gates.

Pass ``--database`` and ``--profile-dir`` to replay a specific live instance.
The SQLite read transaction includes its WAL, so all rows come from one snapshot.
The output describes decisions and proxy conflicts; it does not invent truth labels.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.engine import make_url

from smartapply.config import get_settings
from smartapply.filtering import (
    CandidateQualifications,
    DeterministicRejector,
    ruleset_from_preferences,
)
from smartapply.filtering.hard_reject.input_fingerprint import hard_reject_input_sha256
from smartapply.filtering.hard_reject.role_families import (
    TITLE_FAMILIES,
    match_title_families,
)
from smartapply.filtering.text import norm
from smartapply.profile import get_profile

GATES = ("language", "contract", "experience", "qualification", "seniority", "role")
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "filter-audit-20261009"


def _obj(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot_rows(database: Path) -> tuple[list[sqlite3.Row], dict]:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        rows = connection.execute(
            """SELECT j.id, j.title, j.company, j.description, j.location,
                      j.contract_type, j.remote_policy, j.application_url,
                      j.source, j.source_data, j.status, j.canonical_job_id,
                      j.duplicate_review_status, j.scraped_at,
                      s.components, a.status AS application_status,
                      a.form_submitted_at, x.fit_score
               FROM jobs j
               LEFT JOIN job_scores s ON s.job_id=j.id
               LEFT JOIN applications a ON a.job_id=j.id
               LEFT JOIN job_analyses x ON x.job_id=j.id
               ORDER BY j.id"""
        ).fetchall()
        metadata = {
            "database": str(database),
            "database_size_bytes": database.stat().st_size,
            "snapshot_utc": datetime.now(timezone.utc).isoformat(),
            "row_count": len(rows),
            "sqlite_schema_version": connection.execute("PRAGMA schema_version").fetchone()[0],
        }
        connection.execute("ROLLBACK")
        return rows, metadata
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path)
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    database_url = make_url(get_settings().database_url)
    if not args.database and (
        database_url.get_backend_name() != "sqlite" or not database_url.database
    ):
        raise RuntimeError("This read-only replay requires a file-backed SQLite database")
    database = (args.database or Path(str(database_url.database))).expanduser().resolve()
    profile_dir = (args.profile_dir or get_settings().profile_dir).expanduser().resolve()
    output = args.output.expanduser().resolve()
    rows, metadata = _snapshot_rows(database)
    profile = get_profile(profile_dir)
    rejector = DeterministicRejector(
        ruleset_from_preferences(profile.preferences),
        qualifications=CandidateQualifications.from_profile(profile),
    )
    metadata["profile_dir"] = str(profile_dir)
    metadata["candidate_has_doctorate"] = rejector.qualifications.has_doctorate
    metadata["rules"] = {
        "accepted_job_languages": rejector.rules.accepted_job_languages,
        "accepted_contract_types": rejector.rules.accepted_contract_types,
        "max_required_years": rejector.rules.max_required_years,
        "deal_breakers": rejector.rules.deal_breakers,
    }
    metadata["code_sha256"] = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "smartapply/filtering/hard_reject").glob("*.py"))
    }
    metadata["profile_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(profile_dir.glob("*.json"))
    }

    output.mkdir(parents=True, exist_ok=True)
    status_counts: Counter[str] = Counter()
    old_counts: Counter[str] = Counter()
    gate_status: dict[str, Counter[str]] = {gate: Counter() for gate in GATES}
    gate_by_status: dict[str, dict[str, Counter[str]]] = {
        gate: defaultdict(Counter) for gate in GATES
    }
    gate_by_old: dict[str, dict[str, Counter[str]]] = {gate: defaultdict(Counter) for gate in GATES}
    unique_blockers: Counter[str] = Counter()
    positive_control_blockers: Counter[str] = Counter()
    previous_rejected_now_clear: Counter[str] = Counter()
    old_clear_now_blocked: Counter[str] = Counter()
    pair_overlap: Counter[str] = Counter()
    family_counts: dict[str, Counter[str]] = {family.name: Counter() for family in TITLE_FAMILIES}
    reason_counts: dict[str, Counter[str]] = {gate: Counter() for gate in GATES}
    total_rejected = 0
    positive_control_total = 0
    missing_score = 0
    skipped_existing = 0

    columns = [
        "id",
        "status",
        "application_status",
        "form_submitted",
        "positive_control",
        "title",
        "company",
        "source",
        "canonical_job_id",
        "duplicate_review_status",
        "old_disposition",
        "old_rejection_stage",
        "old_hard_reject_skipped",
        "fit_score",
        "input_sha256",
        "replayed_rejected",
        "blocked_gates",
        "unique_blocker",
        "role_families",
        *GATES,
    ]
    with (
        (output / "offers.csv").open("w", newline="", encoding="utf-8") as summary_file,
        (output / "decisions.jsonl").open("w", encoding="utf-8") as detail_file,
    ):
        writer = csv.DictWriter(summary_file, fieldnames=columns)
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            components = _obj(row["components"])
            if row["components"] is None:
                missing_score += 1
            old = str(components.get("filter_disposition") or "missing")
            old_counts[old] += 1
            old_proof = components.get("hard_reject")
            if isinstance(old_proof, dict) and old_proof.get("skipped"):
                skipped_existing += 1
            job = SimpleNamespace(
                id=row["id"],
                title=row["title"] or "",
                company=row["company"] or "",
                description=row["description"] or "",
                location=row["location"],
                contract_type=row["contract_type"],
                remote_policy=row["remote_policy"],
                application_url=row["application_url"],
                source=row["source"] or "",
                source_data=_obj(row["source_data"]),
            )
            result = rejector.evaluate(job)
            decisions = {decision.gate: decision for decision in result.decisions}
            if set(decisions) != set(GATES):
                raise RuntimeError(f"Incomplete gates for offer {row['id']}: {set(decisions)}")
            status = str(row["status"] or "missing")
            status_counts[status] += 1
            positive_control = bool(
                row["form_submitted_at"]
                or status in {"sent", "ready_for_form_submission", "interview"}
                or row["application_status"] in {"sent", "ready_for_form_submission"}
            )
            positive_control_total += positive_control
            blocked = [gate for gate in GATES if decisions[gate].blocked]
            total_rejected += result.rejected
            if len(blocked) == 1:
                unique_blockers[blocked[0]] += 1
            for left_index, left in enumerate(blocked):
                for right in blocked[left_index + 1 :]:
                    pair_overlap[f"{left}+{right}"] += 1
            families = match_title_families(norm(job.title))
            for family in families:
                family_counts[family.name]["matched"] += 1
                family_counts[family.name]["role_blocked"] += decisions["role"].blocked
                family_counts[family.name]["family_reason_blocked"] += (
                    f"off_target_role_family:{family.name}" in decisions["role"].reason_codes
                )
                family_counts[family.name]["role_unresolved"] += (
                    decisions["role"].status.value == "unresolved"
                )
                family_counts[family.name]["positive_control"] += positive_control
            if result.rejected and old != "rejected":
                old_clear_now_blocked[old] += 1
            if not result.rejected and old == "rejected":
                previous_rejected_now_clear[status] += 1
            for gate in GATES:
                decision = decisions[gate]
                gate_status[gate][decision.status.value] += 1
                gate_by_status[gate][status][decision.status.value] += 1
                gate_by_old[gate][old][decision.status.value] += 1
                if decision.blocked and positive_control:
                    positive_control_blockers[gate] += 1
                if decision.blocked:
                    reason_counts[gate].update(decision.reason_codes)
            item = {
                "id": row["id"],
                "status": status,
                "title": job.title,
                "company": job.company,
                "source": job.source,
                "canonical_job_id": row["canonical_job_id"],
                "positive_control": positive_control,
                "old_disposition": old,
                "replayed_disposition": result.disposition.value,
                "input_sha256": hard_reject_input_sha256(job),
                "gates": [decision.to_dict() for decision in result.decisions],
            }
            detail_file.write(json.dumps(item, ensure_ascii=False) + "\n")
            writer.writerow(
                {
                    "id": row["id"],
                    "status": status,
                    "application_status": row["application_status"] or "",
                    "form_submitted": bool(row["form_submitted_at"]),
                    "positive_control": positive_control,
                    "title": job.title,
                    "company": job.company,
                    "source": job.source,
                    "canonical_job_id": row["canonical_job_id"] or "",
                    "duplicate_review_status": row["duplicate_review_status"] or "",
                    "old_disposition": old,
                    "old_rejection_stage": components.get("rejection_stage") or "",
                    "old_hard_reject_skipped": bool(
                        isinstance(old_proof, dict) and old_proof.get("skipped")
                    ),
                    "fit_score": row["fit_score"] if row["fit_score"] is not None else "",
                    "input_sha256": item["input_sha256"],
                    "replayed_rejected": result.rejected,
                    "blocked_gates": "|".join(blocked),
                    "unique_blocker": blocked[0] if len(blocked) == 1 else "",
                    "role_families": "|".join(family.name for family in families),
                    **{gate: decisions[gate].status.value for gate in GATES},
                }
            )
            if index % 1000 == 0:
                print(f"Replayed {index}/{len(rows)} offers", flush=True)

    report = {
        "metadata": metadata,
        "coverage": {
            "all_jobs": len(rows),
            "by_status": dict(status_counts),
            "with_score_missing": missing_score,
            "existing_skipped": skipped_existing,
            "old_disposition": dict(old_counts),
            "replayed_rejected": total_rejected,
            "replayed_kept": len(rows) - total_rejected,
            "positive_control_total": positive_control_total,
        },
        "gates": {
            gate: {
                "statuses": dict(gate_status[gate]),
                "unique_blocker": unique_blockers[gate],
                "positive_control_blocked": positive_control_blockers[gate],
                "by_job_status": {key: dict(value) for key, value in gate_by_status[gate].items()},
                "by_old_disposition": {
                    key: dict(value) for key, value in gate_by_old[gate].items()
                },
                "reason_codes": dict(reason_counts[gate]),
            }
            for gate in GATES
        },
        "role_families": {
            family.name: {
                "policy": family.policy.value,
                "matched": family_counts[family.name]["matched"],
                "role_blocked": family_counts[family.name]["role_blocked"],
                "family_reason_blocked": family_counts[family.name]["family_reason_blocked"],
                "role_unresolved": family_counts[family.name]["role_unresolved"],
                "positive_control": family_counts[family.name]["positive_control"],
            }
            for family in TITLE_FAMILIES
        },
        "overlap": dict(pair_overlap),
        "old_clear_now_blocked": dict(old_clear_now_blocked),
        "previous_rejected_now_clear_by_status": dict(previous_rejected_now_clear),
    }
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Audit written to {output}")


if __name__ == "__main__":
    main()
