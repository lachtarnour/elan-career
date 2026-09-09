"""Reproduce the reviewed company order; never connect to the application database."""

from __future__ import annotations

import argparse
import csv
import io
import json
import unicodedata
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "data/company-targeting/companies.csv"
AUDIT = ROOT / "data/company-targeting/ranking.json"
LABELS = {
    1: "À candidater maintenant",
    2: "Très bonne cible",
    3: "Bonne cible",
    4: "Secondaire",
    5: "Faible priorité",
}
BANDS = {
    "apply": 1,
    "qualify_offer": 2,
    "targeted_watch": 3,
    "broaden": 4,
    "historical_or_unverified": 5,
}
FIT = {"direct": 0, "transferable": 1, "indirect": 2, "unknown": 3}
WORK = {"research": 0, "applied_ml": 1, "unknown": 2}
MATURITY = {"documented_structure": 0, "not_reviewed": 1}
EVIDENCE = {"offer_read": 0, "employer_page": 1, "search_only": 2, "legacy": 3}
CONSTRAINT = {
    "none_established": 0,
    "contract_mismatch": 1,
    "seniority_gap": 1,
    "unavailable_reference": 1,
    "no_matching_role_seen": 1,
}


def name_key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def sort_key(entry: dict) -> tuple:
    # Equal judgements are ordered alphabetically, not by invented precision.
    return (
        BANDS[entry["action"]],
        CONSTRAINT[entry["constraint"]],
        FIT[entry["fit"]],
        WORK[entry["work"]],
        MATURITY[entry["maturity"]],
        EVIDENCE[entry["evidence"]],
        name_key(entry["company"]),
    )


def render(csv_text: str, audit: dict) -> bytes:
    date.fromisoformat(audit["as_of"])
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    rows = list(reader)
    entries = audit["companies"]
    keys = [name_key(row["Entreprise"]) for row in rows]
    audit_keys = [name_key(entry["company"]) for entry in entries]
    if len(set(keys)) != len(keys) or len(set(audit_keys)) != len(audit_keys):
        raise ValueError("Duplicate company in CSV or audit")
    if not rows or set(keys) != set(audit_keys):
        raise ValueError("CSV and audit must contain exactly the same companies")
    by_name = dict(zip(keys, rows, strict=True))
    for entry in entries:
        sort_key(entry)  # Validate every categorical judgement before writing.
        if not entry["reason"].strip() or not entry["fit_basis"].strip():
            raise ValueError(f"Missing justification: {entry['company']}")
        if entry["evidence"] != "legacy" and not entry["sources"]:
            raise ValueError(f"Missing source: {entry['company']}")
        if entry["action"] == "apply":
            gates = entry.get("eligibility", {})
            if entry["evidence"] != "offer_read" or not all(
                gates.get(key) is True
                for key in (
                    "open",
                    "france",
                    "permanent",
                    "experience_compatible",
                    "no_required_phd",
                )
            ):
                raise ValueError(f"Unverified immediate priority: {entry['company']}")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator="\r\n")
    writer.writeheader()
    for order, entry in enumerate(sorted(entries, key=sort_key), start=1):
        row = by_name[name_key(entry["company"])].copy()
        priority = BANDS[entry["action"]]
        row.update(
            {
                "Ordre": str(order),
                "Priorité": str(priority),
                "Niveau de priorité": LABELS[priority],
                "Raison du classement": entry["reason"],
            }
        )
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--audit", type=Path, default=AUDIT)
    parser.add_argument("--check", action="store_true", help="Verify without writing")
    args = parser.parse_args()
    original = args.csv.read_bytes()
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    result = render(original.decode("utf-8-sig"), audit)
    if args.check:
        if result != original:
            raise SystemExit("CSV differs from the reviewed ranking")
        print(f"Ranking verified: {len(audit['companies'])} companies, review {audit['as_of']}")
    else:
        if result != original:
            temporary = args.csv.with_suffix(args.csv.suffix + ".tmp")
            temporary.write_bytes(result)
            temporary.replace(args.csv)
        print(f"Ranking written: {args.csv}; database not touched")


if __name__ == "__main__":
    main()
