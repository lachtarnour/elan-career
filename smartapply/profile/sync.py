"""Validate and copy profile JSON files, keeping a backup before replacements."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from smartapply.profile.loader import OPTIONAL_FILES, REQUIRED_FILES, clear_cache
from smartapply.profile.schema import Profile

PROFILE_FILES = {**REQUIRED_FILES, **OPTIONAL_FILES}


@dataclass(frozen=True)
class ProfileSyncResult:
    source: Path
    destination: Path
    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    retained: tuple[str, ...]
    backup: Path | None = None


def _read_files(directory: Path) -> dict[str, bytes]:
    files = {}
    for name in PROFILE_FILES.values():
        path = directory / name
        if path.is_symlink():
            raise ValueError(f"Profile file must not be a symbolic link: {path}")
        if path.exists():
            files[name] = path.read_bytes()
    return files


def _validate_files(files: dict[str, bytes], label: str) -> None:
    missing = [name for name in REQUIRED_FILES.values() if name not in files]
    if missing:
        raise ValueError(f"{label}: missing required files: {', '.join(missing)}")
    try:
        data = {
            key: json.loads(files[name]) for key, name in PROFILE_FILES.items() if name in files
        }
        Profile.model_validate(data)
    except ValueError as exc:
        raise ValueError(f"{label}: invalid profile JSON or schema; no files copied") from exc


def _same_json(left: bytes, right: bytes) -> bool:
    try:
        return json.loads(left) == json.loads(right)
    except ValueError:
        return False


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace one file without leaving partially written JSON."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_profile(
    source: Path | str,
    destination: Path | str,
    *,
    dry_run: bool = False,
) -> ProfileSyncResult:
    """Copy source files in one direction; never merge or delete individual records.

    Only known profile files are copied. Optional files absent from the source
    remain in the destination. Both the source and resulting profile must be
    valid before any write. Backups contain the destination's original files.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if source == destination:
        raise ValueError("Source and destination are the same profile directory")
    if not source.is_dir():
        raise ValueError(f"Source profile directory does not exist: {source}")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Destination is not a directory: {destination}")

    incoming = _read_files(source)
    original = _read_files(destination)
    _validate_files(incoming, "Source")
    _validate_files({**original, **incoming}, "Result")

    created = tuple(sorted(incoming.keys() - original.keys()))
    updated = tuple(
        sorted(
            name
            for name in incoming.keys() & original.keys()
            if not _same_json(incoming[name], original[name])
        )
    )
    unchanged = tuple(sorted(incoming.keys() & original.keys() - set(updated)))
    retained = tuple(sorted(original.keys() - incoming.keys()))
    result = ProfileSyncResult(source, destination, created, updated, unchanged, retained)
    changes = created + updated
    if dry_run or not changes:
        return result

    destination.mkdir(parents=True, exist_ok=True)
    backup_root = destination / ".sync-backups"
    backup_root.mkdir(mode=0o700, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
    backup = Path(tempfile.mkdtemp(prefix=timestamp, dir=backup_root))
    for name, content in original.items():
        (backup / name).write_bytes(content)
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "destination": str(destination),
                "created": created,
                "updated": updated,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if _read_files(destination) != original:
        raise ValueError(f"Destination changed during synchronization; retry. Backup: {backup}")

    written = []
    try:
        for name in changes:
            _atomic_write(destination / name, incoming[name])
            written.append(name)
        _validate_files(_read_files(destination), "Written profile")
    except Exception:
        for name in reversed(written):
            if name in original:
                _atomic_write(destination / name, original[name])
            else:
                (destination / name).unlink(missing_ok=True)
        raise
    finally:
        clear_cache()

    return ProfileSyncResult(source, destination, created, updated, unchanged, retained, backup)
