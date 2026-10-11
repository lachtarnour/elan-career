"""Durable recovery for application directory replacement.

The prepared record is committed before either rename. Its committed flag is
updated in the same SQLite transaction as the application and document rows.
Recovery therefore follows the database decision, not the presence of a path.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import AppSetting
from smartapply.logging_setup import get_logger

PUBLICATION_PREFIX = "pending_document_publication:"
_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_MARKER_PREFIX = ".elan-publication-"
logger = get_logger(__name__)


def _owned_path(path: Path, *, parent: Path, name: str) -> bool:
    return path == parent / name and not path.is_symlink()


def _validate(key: str, data: dict) -> tuple[int, Path, Path, Path, str]:
    token = key.removeprefix(PUBLICATION_PREFIX)
    if not _TOKEN_RE.fullmatch(token) or not isinstance(data, dict):
        raise ValueError("Invalid application publication journal")
    application_id = data.get("application_id")
    if not isinstance(application_id, int) or application_id <= 0:
        raise ValueError("Invalid application ID in publication journal")
    final = Path(data["final_dir"])
    staging = Path(data["staging_dir"])
    backup = Path(data["backup_dir"])
    parent = final.parent
    if (
        not final.is_absolute()
        or final.name in {"", ".", ".."}
        or final.is_symlink()
        or not _owned_path(staging, parent=parent, name=f".{final.name}.staging-{token}")
        or not _owned_path(backup, parent=parent, name=f".{final.name}.backup-{token}")
        or type(data.get("had_previous")) is not bool
        or type(data.get("committed")) is not bool
        or not isinstance(data.get("files"), list)
        or not data["files"]
        or not all(
            isinstance(name, str) and name not in {"", ".", ".."}
            and Path(name).name == name and not name.startswith(_MARKER_PREFIX)
            for name in data["files"]
        )
    ):
        raise ValueError("Unsafe application publication journal")
    return application_id, final, staging, backup, token


def _belongs_to(path: Path, token: str) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    marker = path / f"{_MARKER_PREFIX}{token}"
    return marker.is_file() and not marker.is_symlink() and marker.read_text() == token


def _remove_owned_staging(staging: Path, token: str) -> None:
    if staging.exists():
        if not _belongs_to(staging, token):
            raise RuntimeError("Staging directory no longer matches its publication")
        shutil.rmtree(staging)


def _finish_record(key: str, expected: str) -> None:
    with write_session_scope(notify=False) as session:
        setting = session.get(AppSetting, key)
        if setting is None or setting.value != expected:
            raise RuntimeError("Publication journal changed during recovery")
        session.delete(setting)


def recover_record(key: str, raw: str) -> None:
    """Reconcile a single publication while its application lock is held."""
    data = json.loads(raw)
    _, final, staging, backup, token = _validate(key, data)
    has_old = data["had_previous"]
    if data["committed"]:
        if not _belongs_to(final, token) or any(
            not (final / name).is_file() for name in data["files"]
        ):
            raise RuntimeError("Committed application files are missing or changed")
        if backup.exists():
            if not has_old or not backup.is_dir() or backup.is_symlink():
                raise RuntimeError("Unexpected application backup")
            shutil.rmtree(backup)
        _remove_owned_staging(staging, token)
    else:
        if has_old:
            if backup.exists():
                if not backup.is_dir() or backup.is_symlink():
                    raise RuntimeError("Application backup is not a safe directory")
                if final.exists():
                    if not _belongs_to(final, token):
                        raise RuntimeError("Cannot identify the unpublished application files")
                    shutil.rmtree(final)
                backup.replace(final)
            elif not final.exists() or _belongs_to(final, token):
                raise RuntimeError("The previous application version cannot be restored")
        else:
            if backup.exists():
                raise RuntimeError("Unexpected backup for a new application")
            if final.exists():
                if not _belongs_to(final, token):
                    raise RuntimeError("Cannot identify the unpublished application files")
                shutil.rmtree(final)
        _remove_owned_staging(staging, token)
    _finish_record(key, raw)


def pending_records(application_id: int | None = None) -> list[tuple[str, str]]:
    with session_scope() as session:
        records = list(session.execute(select(AppSetting.key, AppSetting.value).where(
            AppSetting.key.startswith(PUBLICATION_PREFIX)
        )))
    if application_id is None:
        return records
    selected = []
    for key, raw in records:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid application publication journal")
        if data.get("application_id") == application_id:
            selected.append((key, raw))
    return selected


def recover_application_publication(application_id: int) -> None:
    """The caller already owns application_publication for this ID."""
    for key, raw in pending_records(application_id):
        recover_record(key, raw)


def recover_application_publications() -> None:
    """Reconcile interrupted publications during database initialization."""
    from smartapply.concurrency import TaskConflictError, application_publication

    for key, raw in pending_records():
        try:
            data = json.loads(raw)
            application_id, *_ = _validate(key, data)
            with application_publication(0, application_id):
                with session_scope() as session:
                    current = session.get(AppSetting, key)
                    current_value = current.value if current is not None else None
                if current_value is not None:
                    recover_record(key, current_value)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError,
                SQLAlchemyError, TaskConflictError):
            logger.exception("Application publication recovery remains pending: %s", key)


class ApplicationPublicationJournal:
    def __init__(
        self, application_id: int, final_dir: Path, staging_dir: Path,
        backup_dir: Path, token: str,
    ):
        self.key = PUBLICATION_PREFIX + token
        self.application_id = application_id
        self.final_dir = final_dir
        self.staging_dir = staging_dir
        self.backup_dir = backup_dir
        self.token = token
        self.prepared = False

    def prepare(self, filenames: list[str]) -> None:
        marker = self.staging_dir / f"{_MARKER_PREFIX}{self.token}"
        marker.write_text(self.token, encoding="ascii")
        data = {
            "application_id": self.application_id,
            "final_dir": str(self.final_dir),
            "staging_dir": str(self.staging_dir),
            "backup_dir": str(self.backup_dir),
            "had_previous": self.final_dir.exists(),
            "committed": False,
            "files": filenames,
        }
        _validate(self.key, data)
        with write_session_scope(notify=False) as session:
            session.add(AppSetting(key=self.key, value=json.dumps(data)))
        self.prepared = True

    def mark_committed(self, session) -> None:
        setting = session.get(AppSetting, self.key)
        if setting is None:
            raise RuntimeError("Application publication journal is missing")
        data = json.loads(setting.value)
        data["committed"] = True
        setting.value = json.dumps(data)

    def recover(self) -> None:
        if not self.prepared:
            return
        with session_scope() as session:
            setting = session.get(AppSetting, self.key)
            raw = setting.value if setting is not None else None
        if raw is not None:
            recover_record(self.key, raw)
        self.prepared = False
