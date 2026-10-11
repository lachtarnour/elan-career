"""Restore documented archive provenance after historical duplicate merges.

Run without --apply to inspect candidates. Applying requires a fresh, verified
SQLite backup. Offers without one matching, documented archive event are left
untouched; this script never infers an archive motive from filter diagnostics.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, joinedload

from smartapply.database.models import Job, JobStatus
from smartapply.database.repository.duplicates import _archived_application_origin
from smartapply.desktop.archive_presentation import legacy_archive_source
from smartapply.jobsearch.archive_reasons import (
    ARCHIVE_ORIGINS,
    archive_origin_from_components,
    archive_rejection_reasons,
)


def candidates(session: Session) -> list[tuple[Job, Job]]:
    """Return roots with no cause and exactly one evidenced source event."""
    found = []
    roots = session.scalars(
        select(Job).where(
            Job.status == JobStatus.ARCHIVED, Job.canonical_job_id.is_(None)
        ).options(joinedload(Job.application), joinedload(Job.score))
    )
    for job in roots:
        application = job.application
        if application is None or application.status != JobStatus.ARCHIVED:
            continue
        components = job.score.components if job.score else {}
        components = components if isinstance(components, dict) else {}
        if (
            components.get("archive_inheritance") is not None
            or archive_rejection_reasons(components)
            or archive_origin_from_components(components) is not None
        ):
            continue
        source = legacy_archive_source(session, job)
        if source is not None:
            found.append((job, source))
    return found


def verified_backup(database: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source,
        sqlite3.connect(destination) as backup,
    ):
        source.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
        if backup.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Backup foreign key check failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    database = args.database.expanduser().resolve(strict=True)
    if args.apply and args.backup is None:
        parser.error("--apply requires --backup")
    if args.backup is not None and args.backup.resolve() == database:
        parser.error("backup must differ from database")

    engine = create_engine(f"sqlite:///{database}", connect_args={"timeout": 30})
    with Session(engine) as session:
        if args.apply:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        found = candidates(session)
        print(f"Documented legacy dossiers: {len(found)}")
        for job, source in found:
            print(f"job={job.id} application={job.application.id} source={source.id}")
        if not args.apply:
            return
        verified_backup(database, args.backup)
        for job, source in found:
            provenance = _archived_application_origin(source, job.application)
            components = dict(job.score.components) if job.score and isinstance(job.score.components, dict) else {}
            components["archive_inheritance"] = provenance
            if provenance.get("origin") in ARCHIVE_ORIGINS:
                components["archive_origin"] = provenance["origin"]
            if job.score is None:
                raise RuntimeError(f"Missing score for job {job.id}; no rows committed")
            job.score.components = components
        session.commit()
        print(f"Updated {len(found)} job_scores.components rows; backup: {args.backup}")


if __name__ == "__main__":
    main()
