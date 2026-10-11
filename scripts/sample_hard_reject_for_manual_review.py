#!/usr/bin/env python3
"""Create a reproducible random sample of blocked decisions for manual review."""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = ROOT / "artifacts" / "filter-audit-20261009"
GATES = ("language", "contract", "experience", "qualification", "seniority", "role")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--per-filter", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--exclude-sample", type=Path)
    parser.add_argument("--output", type=Path, default=Path("manual_sample_100_per_filter.csv"))
    args = parser.parse_args()

    audit_dir = args.audit_dir.expanduser().resolve()
    excluded: set[tuple[str, int]] = set()
    if args.exclude_sample:
        with args.exclude_sample.expanduser().resolve().open(encoding="utf-8") as stream:
            excluded = {
                (row["gate"], int(row["offer_id"])) for row in csv.DictReader(stream)
            }
    decisions_by_gate = {gate: [] for gate in GATES}
    with (audit_dir / "decisions.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            for gate in item["gates"]:
                if gate["status"] == "blocked":
                    decisions_by_gate[gate["gate"]].append((item, gate))

    rng = random.Random(args.seed)
    selected = []
    for gate_name in GATES:
        population = decisions_by_gate[gate_name]
        eligible = [
            (item, gate)
            for item, gate in population
            if (gate_name, int(item["id"])) not in excluded
        ]
        chosen = rng.sample(eligible, min(args.per_filter, len(eligible)))
        selected.extend(
            (gate_name, item, gate, len(population), len(eligible))
            for item, gate in chosen
        )

    ids = sorted({item["id"] for _, item, _, _, _ in selected})
    connection = sqlite3.connect(
        f"{args.database.expanduser().resolve().as_uri()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        placeholders = ",".join("?" for _ in ids)
        jobs = {
            row["id"]: dict(row)
            for row in connection.execute(
                f"""SELECT id, title, company, description, location, contract_type,
                           source, status, canonical_job_id
                    FROM jobs WHERE id IN ({placeholders})""",
                ids,
            )
        }
    finally:
        connection.close()

    rows = []
    for gate_name, item, gate, population_size, eligible_population_size in selected:
        job = jobs[item["id"]]
        rows.append(
            {
                "gate": gate_name,
                "offer_id": item["id"],
                "population_size": population_size,
                "eligible_population_size": eligible_population_size,
                "sampling_probability": min(args.per_filter, eligible_population_size)
                / eligible_population_size,
                "status": item["status"],
                "title": job["title"] or "",
                "company": job["company"] or "",
                "location": job["location"] or "",
                "contract_type": job["contract_type"] or "",
                "source": job["source"] or "",
                "canonical_job_id": job["canonical_job_id"] or "",
                "reason_codes": "|".join(gate["reason_codes"]),
                "filter_evidence": " || ".join(
                    f"{e.get('rule_id', '')}: {e.get('quote', '')}" for e in gate["evidence"]
                ),
                "description": job["description"] or "",
                "manual_verdict": "",
                "manual_rationale": "",
            }
        )

    rows.sort(key=lambda row: (GATES.index(row["gate"]), int(row["offer_id"])))
    output = args.output.expanduser()
    if not output.is_absolute():
        output = audit_dir / output
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(
        json.dumps(
            {
                "output": str(output),
                "seed": args.seed,
                "sample_rows": len(rows),
                "by_filter": {
                    gate: sum(row["gate"] == gate for row in rows) for gate in GATES
                },
                "excluded_rows": len(excluded),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
