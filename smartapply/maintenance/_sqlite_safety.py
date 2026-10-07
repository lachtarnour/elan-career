"""Small SQLite safety primitives shared by one-off maintenance operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SQLiteSafetyError(RuntimeError):
    """A database safety check failed."""


class UnsafeOutputPathError(RuntimeError):
    """An output path aliases a protected input or SQLite sidecar."""


def require_safe_output_path(
    output: Path | str | None,
    *,
    protected_paths: tuple[Path | str, ...] = (),
    sqlite_databases: tuple[Path | str, ...] = (),
) -> Path | None:
    """Resolve an optional output and reject destructive input collisions."""
    if output is None:
        return None

    resolved_output = Path(output).expanduser().resolve()
    protected = {
        Path(path).expanduser().resolve()
        for path in protected_paths
    }
    for database in sqlite_databases:
        resolved_database = Path(database).expanduser().resolve()
        protected.add(resolved_database)
        protected.update(
            Path(f"{resolved_database}{suffix}").resolve()
            for suffix in ("-wal", "-shm", "-journal")
        )
    aliases_protected_input = resolved_output in protected or (
        resolved_output.exists()
        and any(
            path.exists() and resolved_output.samefile(path)
            for path in protected
        )
    )
    if aliases_protected_input:
        raise UnsafeOutputPathError(
            f"Output path would overwrite a protected input: {resolved_output}"
        )
    return resolved_output


def resolve_database(database: Path | str) -> Path:
    """Return an existing database as an absolute path."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise SQLiteSafetyError(f"Database does not exist: {path}")
    return path


def open_read_only(database: Path) -> sqlite3.Connection:
    """Open SQLite without permitting accidental writes."""
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def canonical_json(value: Any) -> str:
    """Serialize a value deterministically for audit records and fingerprints."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def fingerprint(value: Any) -> str:
    """Return a stable SHA-256 fingerprint for JSON-compatible data."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """Hash the exact bytes of an audit input."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Capture every user table row count."""
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }


def require_integrity(connection: sqlite3.Connection, *, label: str) -> str:
    """Fail unless SQLite reports a fully consistent database."""
    result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if result.lower() != "ok":
        raise SQLiteSafetyError(f"{label} integrity_check failed: {result}")
    return result


def require_no_table_triggers(
    connection: sqlite3.Connection,
    *,
    tables: tuple[str, ...],
) -> None:
    """Refuse metadata migrations when their target tables have triggers.

    The maintenance operations update only ``jobs`` and ``job_scores``.  A
    trigger attached to either table could silently write offer content or a
    related table while all row counts stay unchanged.  Refusing that schema
    shape makes the later metadata-only assertion meaningful.
    """
    targets = {table.casefold() for table in tables}
    triggers = [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT name, tbl_name FROM sqlite_schema WHERE type = 'trigger' "
            "UNION ALL "
            "SELECT name, tbl_name FROM sqlite_temp_schema WHERE type = 'trigger'"
        )
        if str(row[1]).casefold() in targets
    ]
    if triggers:
        details = ", ".join(
            f"{name} on {table}" for name, table in sorted(triggers)
        )
        raise SQLiteSafetyError(
            f"Metadata migration refuses triggers on target tables: {details}"
        )


def online_backup(database: Path, backup: Path) -> str:
    """Create and validate a WAL-aware online backup.

    The caller is expected to hold ``BEGIN IMMEDIATE`` on its write connection,
    so the source cannot change between its precondition checks and this copy.
    """
    if backup.exists():
        raise SQLiteSafetyError(f"Backup already exists: {backup}")
    try:
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with (
            closing(
                sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
            ) as source,
            closing(sqlite3.connect(backup)) as destination,
        ):
            source.execute("PRAGMA query_only = ON")
            source.execute("PRAGMA busy_timeout = 30000")
            source.backup(destination)
            integrity = require_integrity(destination, label="backup")
        return integrity
    except Exception:
        backup.unlink(missing_ok=True)
        raise


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "SQLiteSafetyError",
    "UnsafeOutputPathError",
    "canonical_json",
    "file_sha256",
    "fingerprint",
    "online_backup",
    "open_read_only",
    "require_integrity",
    "require_no_table_triggers",
    "require_safe_output_path",
    "resolve_database",
    "table_counts",
    "utc_now",
]
