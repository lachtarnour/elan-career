"""Regression coverage for the isolated macOS bundle build helpers."""

from __future__ import annotations

from pathlib import Path

from smartapply.desktop.build_macos import _render_icon, _smoke_test_env


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
    assert env["LLM_PROVIDER"] == env["EMBEDDINGS_PROVIDER"] == "mock"
