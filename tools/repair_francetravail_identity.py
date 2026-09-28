"""Normalize legacy France Travail hashes without removing job history.

Run against a SQLite backup first, then use ``--apply`` for the active database.
The repair swaps hashes with redundant exact-URL aliases where necessary and
keeps every job and application row in place.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from smartapply.offers import make_external_id


@dataclass(frozen=True)
class IdentityChange:
    job_id: int
    old_external_id: str
    new_external_id: str
    alias_id: int | None


def _native_id(source_data: str | None) -> str:
    data = json.loads(source_data) if source_data else {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("id") or "").strip()


def _plan(conn: sqlite3.Connection) -> list[IdentityChange]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, external_id, source_data, canonical_job_id, duplicate_match_type "
        "FROM jobs WHERE source = 'francetravail' ORDER BY id"
    ).fetchall()
    by_external_id = {row["external_id"]: row for row in rows}
    changes: list[IdentityChange] = []
    for row in rows:
        # A same-source alias retains its former hash as an audit trail.
        if row["duplicate_match_type"] == "exact_url":
            continue
        native_id = _native_id(row["source_data"])
        if not native_id:
            continue
        new_external_id = make_external_id("francetravail", native_id)
        if row["external_id"] == new_external_id:
            continue
        owner = by_external_id.get(new_external_id)
        alias_id = None
        if owner is not None:
            if owner["duplicate_match_type"] != "exact_url" or not owner["canonical_job_id"]:
                raise ValueError(f"Conflicting external ID for job {row['id']}")
            if _native_id(owner["source_data"]).casefold() != native_id.casefold():
                raise ValueError(f"Source ID mismatch for job {row['id']}")
            alias_id = int(owner["id"])
        changes.append(
            IdentityChange(int(row["id"]), row["external_id"], new_external_id, alias_id)
        )
    if len({change.new_external_id for change in changes}) != len(changes):
        raise ValueError("Multiple jobs claim the same France Travail source ID")
    return changes


def _apply(conn: sqlite3.Connection, changes: list[IdentityChange]) -> dict[str, int]:
    before_jobs = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    before_applications = conn.execute("SELECT count(*) FROM applications").fetchone()[0]
    usage_updated = 0
    for change in changes:
        if change.alias_id is not None:
            temporary_id = f"francetravail:repair:{change.alias_id}"
            if conn.execute("SELECT 1 FROM jobs WHERE external_id = ?", (temporary_id,)).fetchone():
                raise ValueError(f"Temporary external ID already exists: {temporary_id}")
            conn.execute(
                "UPDATE jobs SET external_id = ? WHERE id = ?",
                (temporary_id, change.alias_id),
            )
        updated = conn.execute(
            "UPDATE jobs SET external_id = ? WHERE id = ? AND external_id = ?",
            (change.new_external_id, change.job_id, change.old_external_id),
        ).rowcount
        if updated != 1:
            raise ValueError(f"Job {change.job_id} changed during the repair")
        if change.alias_id is not None:
            conn.execute(
                "UPDATE jobs SET external_id = ? WHERE id = ? AND external_id = ?",
                (change.old_external_id, change.alias_id, temporary_id),
            )
        usage_updated += conn.execute(
            "UPDATE llm_usage SET job_external_id = ? WHERE job_id = ? AND job_external_id = ?",
            (change.new_external_id, change.job_id, change.old_external_id),
        ).rowcount
    if conn.execute("SELECT count(*) FROM jobs").fetchone()[0] != before_jobs:
        raise ValueError("Job count changed")
    if conn.execute("SELECT count(*) FROM applications").fetchone()[0] != before_applications:
        raise ValueError("Application count changed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Foreign key check failed")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed")
    return {"jobs_normalized": len(changes), "llm_usage_normalized": usage_updated}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect(f"file:{args.database}?mode={mode}", uri=True, timeout=15)
    try:
        changes = _plan(conn)
        summary = {
            "database": str(args.database),
            "jobs_to_normalize": len(changes),
            "aliases_to_preserve": sum(change.alias_id is not None for change in changes),
        }
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
            if _plan(conn) != changes:
                raise ValueError("The database changed after the repair plan was built")
            summary.update(_apply(conn, changes))
            conn.commit()
        print(json.dumps(summary, ensure_ascii=False))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
