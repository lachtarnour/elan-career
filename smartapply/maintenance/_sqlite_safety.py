"""Small SQLite safety primitives shared by one-off maintenance operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smartapply.database.ingest_revision import (
    INGEST_REVISION_KEY,
    INGEST_REVISION_TRIGGER_SQL,
)


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


@dataclass(frozen=True)
class IngestRevisionGuard:
    """Permit exactly the expected revision writes, preserving every other setting."""

    initial_revision: int | None
    settings_digest: str
    initial_updated_at: Any = None

    def additional_writes(self, job_updates: int) -> int:
        return job_updates if self.initial_revision is not None else 0

    def immutable_setting(self, values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        if self.initial_revision is not None and values.get("key") == INGEST_REVISION_KEY:
            values.pop("value", None)
            values.pop("updated_at", None)
        return values

    def verify(self, connection: sqlite3.Connection, *, job_updates: int) -> None:
        settings = _settings_rows(connection)
        if self.initial_revision is not None:
            current = next((row for row in settings if row["key"] == INGEST_REVISION_KEY), None)
            if current is None or current["value"] != str(self.initial_revision + job_updates):
                raise SQLiteSafetyError("Unexpected ingestion revision change during maintenance")
            if job_updates == 0 and current.get("updated_at") != self.initial_updated_at:
                raise SQLiteSafetyError("Unexpected ingestion revision timestamp change")
        if fingerprint([self.immutable_setting(row) for row in settings]) != self.settings_digest:
            raise SQLiteSafetyError("An unrelated application setting changed during maintenance")


def _settings_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'app_settings'"
    ).fetchone() is None:
        return []
    cursor = connection.execute("SELECT * FROM main.app_settings ORDER BY key")
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor]


def require_no_table_triggers(
    connection: sqlite3.Connection,
    *,
    tables: tuple[str, ...],
) -> IngestRevisionGuard:
    """Refuse target triggers except the exact built-in ingestion counters.

    The maintenance operations update only ``jobs`` and ``job_scores``.
    Only recognized main-schema definitions may update the dedicated revision
    entry. Triggers on app_settings are also refused because they could turn
    that otherwise bounded write into an unrelated mutation. TEMP triggers and
    altered homonyms never receive this exception.
    """
    targets = {table.casefold() for table in tables}
    triggers = [
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in connection.execute(
            "SELECT 'main', name, tbl_name, sql FROM sqlite_schema WHERE type = 'trigger' "
            "UNION ALL "
            "SELECT 'temp', name, tbl_name, sql FROM sqlite_temp_schema WHERE type = 'trigger'"
        )
    ]
    recognized = {
        name for schema, name, table, definition in triggers
        if schema == "main" and table == "jobs" and "jobs" in targets
        and INGEST_REVISION_TRIGGER_SQL.get(name) == definition
    }
    if recognized:
        targets.add("app_settings")
    refused = [
        (name, table) for schema, name, table, definition in triggers
        if table.casefold() in targets and not (
            schema == "main" and table == "jobs"
            and INGEST_REVISION_TRIGGER_SQL.get(name) == definition
        )
    ]
    if refused:
        details = ", ".join(
            f"{name} on {table}" for name, table in sorted(refused)
        )
        raise SQLiteSafetyError(
            f"Metadata migration refuses triggers on target tables: {details}"
        )
    settings = _settings_rows(connection)
    initial_revision = None
    initial_updated_at = None
    if "elan_ingest_jobs_update" in recognized:
        counter = next((row for row in settings if row["key"] == INGEST_REVISION_KEY), None)
        value = counter["value"] if counter is not None else None
        if value is not None:
            try:
                initial_revision = int(value)
            except (TypeError, ValueError) as exc:
                raise SQLiteSafetyError("Invalid ingestion revision counter") from exc
            if initial_revision < 0 or str(initial_revision) != value:
                raise SQLiteSafetyError("Invalid ingestion revision counter")
            initial_updated_at = counter.get("updated_at")
    guard = IngestRevisionGuard(initial_revision, "")
    return IngestRevisionGuard(
        initial_revision, fingerprint([guard.immutable_setting(row) for row in settings]),
        initial_updated_at,
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
    "IngestRevisionGuard",
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
