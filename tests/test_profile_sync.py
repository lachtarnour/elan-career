"""Profile synchronization must preserve data and reject invalid replacements."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from smartapply.profile.loader import get_profile, load_profile
from smartapply.profile.sync import sync_profile


@pytest.fixture
def profiles(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "profile"
    source = tmp_path / "project profile"
    destination = tmp_path / "app profile"
    shutil.copytree(fixture, source)
    shutil.copytree(fixture, destination)
    return source, destination


def _edit_json(path, edit):
    data = json.loads(path.read_bytes())
    edit(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _snapshot(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def test_preview_reports_changes_without_writing(profiles):
    source, destination = profiles
    _edit_json(source / "identity.json", lambda data: data.update(title="Updated title"))
    (source / "languages.json").unlink()
    (destination / "template_style.json").unlink()
    original = _snapshot(destination)

    result = sync_profile(source, destination, dry_run=True)

    assert result.updated == ("identity.json",)
    assert result.created == ("template_style.json",)
    assert result.retained == ("languages.json",)
    assert result.backup is None
    assert _snapshot(destination) == original
    assert not (destination / ".sync-backups").exists()


def test_sync_backs_up_and_replaces_whole_files_without_touching_other_data(profiles):
    source, destination = profiles
    _edit_json(source / "identity.json", lambda data: data.update(title="Updated title"))
    _edit_json(source / "projects.json", lambda data: data.pop())
    (source / "languages.json").unlink()
    (destination / "template_style.json").unlink()
    (source / ".env").write_text("SOURCE_SECRET=example")
    (destination / ".env").write_text("DESTINATION_SECRET=example")
    (destination / "local.db").write_bytes(b"unrelated database")
    original = _snapshot(destination)
    cached = get_profile(destination)

    result = sync_profile(source, destination)

    assert result.updated == ("identity.json", "projects.json")
    assert result.backup is not None
    for name in ("identity.json", "projects.json", "template_style.json"):
        assert (destination / name).read_bytes() == (source / name).read_bytes()
    for name in ("languages.json", ".env", "local.db"):
        assert (destination / name).read_bytes() == original[name]
    assert (result.backup / "projects.json").read_bytes() == original["projects.json"]
    assert not (result.backup / ".env").exists()
    assert not (result.backup / "local.db").exists()
    manifest = json.loads((result.backup / "manifest.json").read_bytes())
    assert manifest["created"] == ["template_style.json"]
    assert get_profile(destination).identity.title == "Updated title"
    assert len(load_profile(destination).projects) == len(cached.projects) - 1

    second = sync_profile(source, destination)
    assert second.backup is None
    assert not second.created and not second.updated
    assert len(list((destination / ".sync-backups").iterdir())) == 1


def test_formatting_only_changes_do_not_trigger_a_copy(profiles):
    source, destination = profiles
    path = source / "projects.json"
    path.write_text(json.dumps(json.loads(path.read_bytes()), separators=(",", ":")))
    original = _snapshot(destination)

    result = sync_profile(source, destination)

    assert not result.updated
    assert _snapshot(destination) == original
    assert not (destination / ".sync-backups").exists()


@pytest.mark.parametrize("invalid", ["malformed", "schema", "missing"])
def test_invalid_source_never_changes_destination(profiles, invalid):
    source, destination = profiles
    if invalid == "malformed":
        (source / "projects.json").write_text("not json")
    elif invalid == "schema":
        _edit_json(source / "identity.json", lambda data: data.update(email="invalid"))
    else:
        (source / "identity.json").unlink()
    original = _snapshot(destination)

    with pytest.raises(ValueError):
        sync_profile(source, destination)

    assert _snapshot(destination) == original
    assert not (destination / ".sync-backups").exists()


def test_retained_optional_files_are_validated_with_incoming_profile(profiles):
    source, destination = profiles
    project_bullet_id = load_profile(destination).projects[0].bullets[0].id
    (source / "projects.json").unlink()
    _edit_json(
        source / "experiences.json",
        lambda data: data[0]["bullets"][0].update(id=project_bullet_id),
    )
    load_profile(source)
    original = _snapshot(destination)

    with pytest.raises(ValueError, match="Result"):
        sync_profile(source, destination)

    assert _snapshot(destination) == original
    assert not (destination / ".sync-backups").exists()


def test_failed_write_restores_replaced_files_and_removes_created_files(profiles, monkeypatch):
    import smartapply.profile.sync as module

    source, destination = profiles
    (destination / "template_style.json").unlink()
    _edit_json(source / "identity.json", lambda data: data.update(title="Updated title"))
    _edit_json(source / "projects.json", lambda data: data.pop())
    original = _snapshot(destination)
    real_write = module._atomic_write

    def fail_on_projects(path, content):
        if path.name == "projects.json":
            raise OSError("simulated write failure")
        real_write(path, content)

    monkeypatch.setattr(module, "_atomic_write", fail_on_projects)
    with pytest.raises(OSError, match="simulated write failure"):
        sync_profile(source, destination)

    assert _snapshot(destination) == original
    backup = next((destination / ".sync-backups").iterdir())
    assert (backup / "identity.json").read_bytes() == original["identity.json"]
    load_profile(destination)


def test_can_initialize_missing_destination_but_dry_run_does_not_create_it(profiles, tmp_path):
    source, _ = profiles
    destination = tmp_path / "new profile"

    sync_profile(source, destination, dry_run=True)
    assert not destination.exists()

    sync_profile(source, destination)
    assert load_profile(destination) == load_profile(source)


def test_same_directory_and_symlinked_files_are_rejected(profiles, tmp_path):
    source, destination = profiles
    with pytest.raises(ValueError, match="same profile"):
        sync_profile(source, source)
    outside = tmp_path / "outside.json"
    outside.write_bytes((destination / "projects.json").read_bytes())
    (destination / "projects.json").unlink()
    (destination / "projects.json").symlink_to(outside)
    original = outside.read_bytes()

    with pytest.raises(ValueError, match="symbolic link"):
        sync_profile(source, destination)
    assert outside.read_bytes() == original


def test_cli_direction_defaults_and_preview_ignore_development_profile_override(
    profiles,
    tmp_path,
    monkeypatch,
):
    import smartapply.config as config
    from smartapply.cli import cli

    project, app = profiles
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    app.rename(runtime / "profile")
    app = runtime / "profile"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setenv("PROFILE_DIR", str(project))
    _edit_json(app / "identity.json", lambda data: data.update(title="App title"))
    original = _snapshot(project)
    runner = CliRunner()
    arguments = ["sync-profile", "--project-profile", str(project)]

    preview = runner.invoke(cli, arguments + ["--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert f"Destination: {app}" in preview.output
    assert "Replace: identity.json" in preview.output
    assert "no files written" in preview.output
    assert _snapshot(project) == original

    reverse = runner.invoke(cli, arguments + ["--direction", "from-app"])
    assert reverse.exit_code == 0, reverse.output
    assert "Backup:" in reverse.output
    assert load_profile(project).identity.title == "App title"
    assert load_profile(app).identity.title == "App title"
