"""Regression coverage for evidence-based repair of saved WTTJ contracts."""

from __future__ import annotations

import json
import sqlite3

import pytest

from smartapply.maintenance._sqlite_safety import SQLiteSafetyError
from smartapply.maintenance.wttj_contracts import repair_wttj_contracts
from smartapply.utils.contracts import normalize_source_contract_type


def _database(tmp_path):
    database = tmp_path / "jobs.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, contract_type TEXT, "
            "source_data TEXT, status TEXT, description TEXT)"
        )
        rows = [
            (1, "welcometothejungle", "Full-time", {"detail_api": {"contract_type": "full_time"}}),
            (2, "welcometothejungle", "FULL_TIME", {"matches_api": {"contract_type": "full_time"}}),
            (3, "welcometothejungle", "Full-time", {"employment_type": "FULL_TIME"}),
            (4, "linkedin", "Full-time", {"contractType": "Full-time"}),
            (5, "welcometothejungle", "CDD", {"matches_api": {"contract_type": "full_time"}}),
            (6, "welcometothejungle", "CDI", {"matches_api": {"contract_type": "full_time"}}),
            (7, "welcometothejungle", "Full-time", {}),
            (8, "welcometothejungle", "Full-time", {
                "detail_api": {"contract_type": "temporary"},
                "matches_api": {"contract_type": "full_time"},
            }),
            (9, "welcometothejungle", None, {"matches_api": {"contract_type": "full_time"}}),
        ]
        connection.executemany(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, 'analyzed', 'Original offer text')",
            [(job_id, source, contract, json.dumps(data)) for job_id, source, contract, data in rows],
        )
        connection.execute(
            "INSERT INTO jobs VALUES (10, 'welcometothejungle', 'Full-time', '{invalid', "
            "'analyzed', 'Original offer text')"
        )
    return database


def _rows(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()


def test_repair_changes_only_proven_labels_and_keeps_a_backup(tmp_path):
    database = _database(tmp_path)
    before = _rows(database)
    backup = tmp_path / "before.bak"

    assert repair_wttj_contracts(database, backup=backup) == 3
    assert _rows(backup) == before
    after = _rows(database)
    assert after == [
        (*row[:2], "CDI", *row[3:]) if row[0] in (1, 2, 3) else row
        for row in before
    ]
    second_backup = tmp_path / "second.bak"
    assert repair_wttj_contracts(database, backup=second_backup) == 0
    assert not second_backup.exists()
    assert _rows(database) == after


def test_repair_rolls_back_if_backup_cannot_be_created(tmp_path):
    database = _database(tmp_path)
    before = _rows(database)
    backup = tmp_path / "existing.bak"
    backup.write_bytes(b"existing backup")

    with pytest.raises(SQLiteSafetyError, match="Backup already exists"):
        repair_wttj_contracts(database, backup=backup)

    assert _rows(database) == before
    assert backup.read_bytes() == b"existing backup"


def test_full_time_schedule_keeps_its_generic_meaning():
    assert normalize_source_contract_type("full_time") == "Full-time"
    assert normalize_source_contract_type("Full-time", source="linkedin") == "Full-time"
    assert normalize_source_contract_type("full_time", source="welcometothejungle") == "CDI"
