"""Destructive output-path guards for maintenance commands."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from scripts.reclassify_hard_rejects import main as reclassify_main
from scripts.recover_confirmed_false_rejections import main as recovery_main
from smartapply.maintenance import _sqlite_safety
from smartapply.maintenance._sqlite_safety import (
    UnsafeOutputPathError,
    require_safe_output_path,
)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_output_cannot_alias_database_or_sidecars(tmp_path: Path, suffix: str) -> None:
    database = tmp_path / "smartapply.db"

    with pytest.raises(UnsafeOutputPathError, match="protected input"):
        require_safe_output_path(
            Path(f"{database}{suffix}"),
            sqlite_databases=(database,),
        )


def test_output_cannot_be_a_hardlink_to_database(tmp_path: Path) -> None:
    database = tmp_path / "smartapply.db"
    alias = tmp_path / "report.json"
    database.write_bytes(b"database sentinel")
    alias.hardlink_to(database)

    with pytest.raises(UnsafeOutputPathError, match="protected input"):
        require_safe_output_path(alias, sqlite_databases=(database,))

    assert database.read_bytes() == b"database sentinel"


def test_backup_is_private_before_sqlite_opens_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "smartapply.db"
    backup = tmp_path / "backup.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)")

    original_connect = _sqlite_safety.sqlite3.connect
    observed_modes: list[int] = []

    def inspecting_connect(target, *args, **kwargs):
        if Path(target) == backup:
            observed_modes.append(backup.stat().st_mode & 0o777)
        return original_connect(target, *args, **kwargs)

    monkeypatch.setattr(_sqlite_safety.sqlite3, "connect", inspecting_connect)

    assert _sqlite_safety.online_backup(database, backup) == "ok"
    assert observed_modes == [0o600]


def test_reclassify_cli_refuses_to_overwrite_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "smartapply.db"
    database.write_bytes(b"database sentinel")
    monkeypatch.setattr(
        sys,
        "argv",
        ["reclassify_hard_rejects.py", "--database", str(database), "--output", str(database)],
    )

    with pytest.raises(UnsafeOutputPathError):
        reclassify_main()

    assert database.read_bytes() == b"database sentinel"


def test_reclassify_cli_refuses_to_overwrite_profile_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "smartapply.db"
    profile_dir = tmp_path / "profile"
    preferences = profile_dir / "preferences.json"
    profile_dir.mkdir()
    database.write_bytes(b"database sentinel")
    preferences.write_bytes(b"profile sentinel")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reclassify_hard_rejects.py",
            "--database",
            str(database),
            "--profile-dir",
            str(profile_dir),
            "--output",
            str(preferences),
        ],
    )

    with pytest.raises(UnsafeOutputPathError):
        reclassify_main()

    assert preferences.read_bytes() == b"profile sentinel"


def test_recovery_cli_refuses_to_overwrite_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "smartapply.db"
    manifest = tmp_path / "replay.json"
    database.write_bytes(b"database sentinel")
    manifest.write_bytes(b"manifest sentinel")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recover_confirmed_false_rejections.py",
            "--database",
            str(database),
            "--replay",
            str(manifest),
            "--output",
            str(manifest),
        ],
    )

    with pytest.raises(UnsafeOutputPathError):
        recovery_main()

    assert manifest.read_bytes() == b"manifest sentinel"
