"""Repair known historical application inconsistencies without deleting rows.

The command is read-only by default. Pass ``--apply`` only after reviewing its
JSON plan. It archives secondary live dossiers in confirmed duplicate groups,
repairs unambiguous document paths, and links strict exact-URL aliases.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class ApplicationArchive:
    application_id: int
    job_id: int
    canonical_job_id: int
    old_status: str


@dataclass(frozen=True)
class DocumentPathRepair:
    document_id: int
    application_id: int
    old_path: str
    new_path: str


@dataclass(frozen=True)
class ExactUrlAlias:
    alias_job_id: int
    canonical_job_id: int
    application_url: str


@dataclass(frozen=True)
class RepairPlan:
    applications: tuple[ApplicationArchive, ...]
    documents: tuple[DocumentPathRepair, ...]
    aliases: tuple[ExactUrlAlias, ...]


def _canonical_id(job_id: int, parents: dict[int, int]) -> int:
    seen: set[int] = set()
    while job_id in parents:
        if job_id in seen:
            raise ValueError(f"Canonical cycle involving job {job_id}")
        seen.add(job_id)
        job_id = parents[job_id]
    return job_id


def _application_plan(conn: sqlite3.Connection) -> tuple[ApplicationArchive, ...]:
    parents = {
        int(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT id, canonical_job_id FROM jobs WHERE canonical_job_id IS NOT NULL"
        )
    }
    jobs = {
        int(row[0]): row
        for row in conn.execute("SELECT id, status FROM jobs")
    }
    by_root: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT id, job_id, status FROM applications WHERE status != 'archived' ORDER BY id"
    ):
        by_root.setdefault(_canonical_id(int(row["job_id"]), parents), []).append(row)

    changes: list[ApplicationArchive] = []
    for root_id, applications in by_root.items():
        if len(applications) < 2:
            continue
        root_applications = [row for row in applications if int(row["job_id"]) == root_id]
        if len(root_applications) != 1:
            raise ValueError(
                f"Duplicate group {root_id} has several live dossiers and no unique root dossier"
            )
        for row in applications:
            job_id = int(row["job_id"])
            if job_id == root_id:
                continue
            if jobs[job_id]["status"] != "archived":
                raise ValueError(f"Secondary job {job_id} is still active; manual review required")
            changes.append(
                ApplicationArchive(
                    application_id=int(row["id"]),
                    job_id=job_id,
                    canonical_job_id=root_id,
                    old_status=str(row["status"]),
                )
            )
    return tuple(changes)


def _normal(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _path_candidate(doc_type: str, old_path: str) -> str | None:
    path = Path(old_path)
    if path.exists() or not path.parent.is_dir():
        return None
    prefix = "cv -" if doc_type.startswith("cv_") else "motivation -"
    candidates = [
        item
        for item in path.parent.iterdir()
        if item.is_file()
        and item.suffix.casefold() == path.suffix.casefold()
        and _normal(item.name).startswith(prefix)
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"Document {old_path!r} has {len(candidates)} possible replacements"
        )
    return str(candidates[0])


def _document_plan(conn: sqlite3.Connection) -> tuple[DocumentPathRepair, ...]:
    changes: list[DocumentPathRepair] = []
    for row in conn.execute(
        "SELECT id, application_id, doc_type, path FROM generated_documents "
        "WHERE path IS NOT NULL ORDER BY id"
    ):
        candidate = _path_candidate(str(row["doc_type"]), str(row["path"]))
        if candidate is not None:
            changes.append(
                DocumentPathRepair(
                    document_id=int(row["id"]),
                    application_id=int(row["application_id"]),
                    old_path=str(row["path"]),
                    new_path=candidate,
                )
            )
    return tuple(changes)


def _normalized_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, parsed.query, ""))


def _label(value: str, *, compact: bool = False) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", "" if compact else " ", text).strip()


def _exact_url_alias_plan(conn: sqlite3.Connection) -> tuple[ExactUrlAlias, ...]:
    independent = conn.execute(
        "SELECT j.id, j.company, j.title, j.application_url, j.status, a.id AS application_id "
        "FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE j.canonical_job_id IS NULL AND j.application_url IS NOT NULL ORDER BY j.id"
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in independent:
        groups.setdefault(_normalized_url(str(row["application_url"])), []).append(row)

    changes: list[ExactUrlAlias] = []
    for url, rows in groups.items():
        if len(rows) != 2:
            continue
        first, second = rows
        if _label(first["title"]) != _label(second["title"]):
            continue
        if _label(first["company"], compact=True) != _label(second["company"], compact=True):
            continue
        with_application = [row for row in rows if row["application_id"] is not None]
        without_application = [row for row in rows if row["application_id"] is None]
        if len(with_application) != 1 or len(without_application) != 1:
            continue
        canonical, alias = with_application[0], without_application[0]
        if alias["status"] != "archived":
            raise ValueError(f"Exact-URL alias candidate {alias['id']} is not archived")
        changes.append(
            ExactUrlAlias(
                alias_job_id=int(alias["id"]),
                canonical_job_id=int(canonical["id"]),
                application_url=url,
            )
        )
    return tuple(changes)


def build_plan(conn: sqlite3.Connection) -> RepairPlan:
    conn.row_factory = sqlite3.Row
    return RepairPlan(
        applications=_application_plan(conn),
        documents=_document_plan(conn),
        aliases=_exact_url_alias_plan(conn),
    )


def apply_plan(conn: sqlite3.Connection, plan: RepairPlan) -> None:
    counts_before = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("jobs", "applications", "generated_documents")
    }
    for change in plan.applications:
        updated = conn.execute(
            "UPDATE applications SET status = 'archived' "
            "WHERE id = ? AND job_id = ? AND status = ?",
            (change.application_id, change.job_id, change.old_status),
        ).rowcount
        if updated != 1:
            raise ValueError(f"Application {change.application_id} changed after planning")
    for change in plan.documents:
        updated = conn.execute(
            "UPDATE generated_documents SET path = ? WHERE id = ? AND path = ?",
            (change.new_path, change.document_id, change.old_path),
        ).rowcount
        if updated != 1:
            raise ValueError(f"Document {change.document_id} changed after planning")
        conn.execute(
            "UPDATE applications SET cv_docx_path = ? "
            "WHERE id = ? AND cv_docx_path = ?",
            (change.new_path, change.application_id, change.old_path),
        )
        conn.execute(
            "UPDATE applications SET cv_pdf_path = ? WHERE id = ? AND cv_pdf_path = ?",
            (change.new_path, change.application_id, change.old_path),
        )
    for change in plan.aliases:
        updated = conn.execute(
            "UPDATE jobs SET canonical_job_id = ?, possible_duplicate_of_id = NULL, "
            "duplicate_review_status = 'confirmed', duplicate_match_type = 'exact_url', "
            "duplicate_confidence = 1.0 WHERE id = ? AND canonical_job_id IS NULL",
            (change.canonical_job_id, change.alias_job_id),
        ).rowcount
        if updated != 1:
            raise ValueError(f"Job {change.alias_job_id} changed after planning")

    for table, count in counts_before.items():
        if conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != count:
            raise ValueError(f"{table} row count changed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Foreign key check failed")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed")


def _summary(database: Path, plan: RepairPlan, applied: bool) -> dict[str, object]:
    return {
        "database": str(database),
        "applied": applied,
        "application_archives": [asdict(item) for item in plan.applications],
        "document_path_repairs": len(plan.documents),
        "exact_url_aliases": [asdict(item) for item in plan.aliases],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect(f"file:{args.database}?mode={mode}", uri=True, timeout=15)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        plan = build_plan(conn)
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
            if build_plan(conn) != plan:
                raise ValueError("The database changed after the repair plan was built")
            apply_plan(conn, plan)
            conn.commit()
        print(json.dumps(_summary(args.database, plan, args.apply), ensure_ascii=False, indent=2))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
