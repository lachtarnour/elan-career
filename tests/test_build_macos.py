"""Regression coverage for the isolated macOS bundle build helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from smartapply.desktop import build_macos
from smartapply.desktop.build_macos import (
    _assert_bundle_not_running,
    _render_icon,
    _running_bundle_pids,
    _smoke_test_env,
)


def test_render_icon_creates_a_real_icns_file(tmp_path: Path) -> None:
    icon = _render_icon(tmp_path)

    assert icon == tmp_path / "Elan.icns"
    assert icon.read_bytes().startswith(b"icns")
    assert icon.stat().st_size > 1_024


def test_smoke_test_env_isolated_from_user_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    env = _smoke_test_env(runtime)

    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert env["ELAN_HOME"] == str(runtime)
    assert env["ELAN_ENV_FILE"] == str(runtime / ".env")
    assert env["DATABASE_URL"] == f"sqlite:///{runtime / 'smartapply.db'}"
    assert env["OUTPUT_DIR"] == str(runtime / "documents")
    assert env["CACHE_DIR"] == str(runtime / "cache")


def test_running_bundle_pids_matches_the_exact_packaged_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "Élan dev" / "Elan"
    run = Mock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="14915\n14916\n", stderr=""
        )
    )
    monkeypatch.setattr(build_macos.subprocess, "run", run)

    assert _running_bundle_pids(executable) == [14915, 14916]
    assert run.call_args.args[0] == [
        "pgrep",
        "-f",
        rf"^{build_macos.re.escape(str(executable))}( |$)",
    ]


def test_build_refuses_to_replace_a_running_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_macos, "_running_bundle_pids", lambda _executable: [14915])

    with pytest.raises(RuntimeError, match="Quit the application before rebuilding"):
        _assert_bundle_not_running()


def test_alternate_build_writes_only_to_the_guarded_destination(monkeypatch, tmp_path):
    destination = tmp_path / "staged" / "Elan.app"
    original = tmp_path / "live" / "Elan.app"
    original.mkdir(parents=True)
    (original / "sentinel").write_text("running bundle")
    monkeypatch.setattr(build_macos, "DIST_APP", destination)
    monkeypatch.setattr(build_macos, "BUILD_DIR", tmp_path / "build")
    monkeypatch.setattr(build_macos, "_render_icon", lambda path: path / "Elan.icns")
    monkeypatch.setattr(build_macos, "_finalize_bundle_metadata", lambda: None)
    guarded = []
    monkeypatch.setattr(build_macos, "_running_bundle_pids", lambda path: guarded.append(path) or [])

    def build(command, **kwargs):
        output = Path(command[command.index("--distpath") + 1]) / "Elan.app"
        output.mkdir(parents=True)

    monkeypatch.setattr(build_macos.subprocess, "run", build)
    assert build_macos.build_bundle(smoke_test=False) == destination
    assert guarded == [destination / "Contents" / "MacOS" / "Elan"]
    assert (original / "sentinel").read_text() == "running bundle"
