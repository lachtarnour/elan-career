"""Repair historical WTTJ contract labels using their saved source evidence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from smartapply.maintenance._sqlite_safety import (
    SQLiteSafetyError,
    online_backup,
    require_no_table_triggers,
    require_safe_output_path,
    resolve_database,
)
from smartapply.utils.contracts import normalize_source_contract_type

_SOURCE = "welcometothejungle"


def repair_wttj_contracts(database: Path | str, *, backup: Path | str) -> int:
    """Back up and atomically replace legacy Full-time labels proven to be CDI.

    Only the contract label changes. Missing, malformed or contradictory source
    evidence is left untouched. A repeated call is a no-op without a new backup.
    """
    database = resolve_database(database)
    backup = require_safe_output_path(backup, sqlite_databases=(database,))
    assert backup is not None

    with closing(
        sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True, timeout=30,
                        isolation_level=None)
    ) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            changes = []
            for job_id, contract_type, source_data in connection.execute(
                "SELECT id, contract_type, source_data FROM jobs WHERE source = ?", (_SOURCE,)
            ):
                if normalize_source_contract_type(contract_type) != "Full-time":
                    continue
                if _source_proves_cdi(source_data):
                    changes.append(("CDI", job_id, contract_type))
            if not changes:
                connection.rollback()
                return 0

            require_no_table_triggers(connection, tables=("jobs",))
            online_backup(database, backup)
            result = connection.executemany(
                "UPDATE jobs SET contract_type = ? WHERE id = ? AND contract_type = ?",
                changes,
            )
            if result.rowcount != len(changes):
                raise SQLiteSafetyError("WTTJ contract repair did not update the expected rows")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SQLiteSafetyError("WTTJ contract repair failed SQLite quick_check")
            connection.commit()
            return len(changes)
        except Exception:
            connection.rollback()
            raise


def _source_proves_cdi(raw_source_data: str | None) -> bool:
    try:
        source_data = json.loads(raw_source_data or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(source_data, dict):
        return False
    values = [source_data.get("employment_type")]
    for source in ("detail_api", "matches_api"):
        payload = source_data.get(source)
        if isinstance(payload, dict):
            values.append(payload.get("contract_type"))
    values = [value for value in values if value is not None and value != ""]
    return bool(values) and all(
        isinstance(value, str)
        and normalize_source_contract_type(value, source=_SOURCE) == "CDI"
        for value in values
    )
