"""Rename the persisted source label without rebuilding jobs or related tables."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import Engine


class SourceCompanyMigrationError(RuntimeError):
    """The existing database cannot be migrated without manual inspection."""


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute('PRAGMA table_info("jobs")')}


def migrate_source_company(engine: Engine) -> Path | None:
    """Rename jobs.company once, saving a consistent SQLite copy first.

    A reserved write lock prevents intervening writes between the backup and
    the schema change. SQLite handles dependent indexes and foreign keys when
    renaming a column; we verify both after the rename, before commit.
    """
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    path = Path(database)
    if not path.exists():
        return None

    with sqlite3.connect(path, timeout=30, isolation_level=None) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns = _columns(connection)
            if not columns:
                connection.execute("ROLLBACK")
                return None
            if "company" in columns and "source_company" in columns:
                raise SourceCompanyMigrationError(
                    "jobs contains both company and source_company; inspect the values before migration"
                )
            if "source_company" in columns:
                connection.execute("ROLLBACK")
                return None
            if "company" not in columns:
                raise SourceCompanyMigrationError("jobs has neither company nor source_company")

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = path.with_name(f"{path.name}.pre-source-company-{stamp}.bak")
            try:
                with sqlite3.connect(backup) as destination:
                    # The backup reader sees the state held by this transaction.
                    with sqlite3.connect(path, timeout=30) as reader:
                        reader.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise SourceCompanyMigrationError("SQLite backup failed integrity_check")
            except Exception:
                backup.unlink(missing_ok=True)
                raise

            before_indexes = {
                row[1] for row in connection.execute('PRAGMA index_list("jobs")')
            }
            before_foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
            if before_foreign_keys:
                raise SourceCompanyMigrationError("Existing foreign key violations; backup retained")
            connection.execute('ALTER TABLE "jobs" RENAME COLUMN "company" TO "source_company"')
            after_indexes = {
                row[1] for row in connection.execute('PRAGMA index_list("jobs")')
            }
            if before_indexes != after_indexes:
                raise SourceCompanyMigrationError("Job indexes changed during rename; backup retained")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SourceCompanyMigrationError("Foreign key violation after rename; backup retained")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SourceCompanyMigrationError("SQLite quick_check failed; backup retained")
            connection.execute("COMMIT")
            engine.dispose()
            return backup
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
