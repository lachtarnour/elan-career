"""Recoverable removal of owned application directories.

Persist the plan before renaming. On restart, an uncommitted deletion restores
its directories; a committed deletion finishes erasing its staged files.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Application, AppSetting, GeneratedDocument

DELETION_PREFIX = "pending_document_deletion:"


def _owned_directory_name(name: str, application_id: int) -> bool:
    return name == str(application_id) or bool(
        re.fullmatch(
            rf"\.{application_id}\.(?:staging|backup)-[0-9a-f]{{32}}",
            name,
        )
    )


def owned_application_directory(session, application: Application, output_dir: Path) -> Path:
    paths = [
        Path(value).expanduser().absolute()
        for value in (
            application.cv_docx_path,
            application.cv_pdf_path,
            *(document.path for document in application.documents),
        )
        if value
    ]
    directory = (
        paths[0].parent if paths else output_dir.expanduser().absolute() / str(application.id)
    )
    if directory.name != str(application.id) or directory.resolve() != directory:
        raise ValueError(
            "Le dossier de candidature n’est pas un répertoire géré sûr. Suppression interrompue."
        )
    if any(path.parent != directory for path in paths):
        raise ValueError(
            "Les documents sont répartis dans plusieurs dossiers. Suppression interrompue."
        )
    if directory.exists() and not directory.is_dir():
        raise ValueError("Le chemin du dossier n’est pas un répertoire.")
    others = list(
        session.scalars(
            select(GeneratedDocument.path).where(
                GeneratedDocument.application_id != application.id,
                GeneratedDocument.path.is_not(None),
            )
        )
    )
    for other in session.scalars(select(Application).where(Application.id != application.id)):
        others.extend(value for value in (other.cv_docx_path, other.cv_pdf_path) if value)
    if any(Path(value).expanduser().resolve().is_relative_to(directory) for value in others):
        raise ValueError("Ce dossier contient des documents d’une autre candidature.")
    return directory


class ApplicationOutputDeletion:
    def __init__(self, directories: dict[int, Path]):
        token = uuid4().hex
        self.key = DELETION_PREFIX + token
        self.entries = [
            {
                "application_id": application_id,
                "directory": str(directory),
                "staged": str(directory.parent / f".{directory.name}.deleting-{token}"),
            }
            for application_id, directory in directories.items()
        ]
        # Interrupted rendering can leave older versions beside the dossier.
        # Publication locks held by the caller exclude active rendering here.
        for application_id, directory in directories.items():
            if not directory.parent.exists():
                continue
            for sibling in directory.parent.iterdir():
                if sibling != directory and _owned_directory_name(sibling.name, application_id):
                    if sibling.resolve() != sibling or not sibling.is_dir():
                        raise ValueError(
                            "Un ancien dossier temporaire n’est pas un répertoire sûr."
                        )
                    self.entries.append(
                        {
                            "application_id": application_id,
                            "directory": str(sibling),
                            "staged": str(sibling.parent / f".{sibling.name}.deleting-{token}"),
                        }
                    )

    def prepare(self) -> None:
        with write_session_scope(notify=False) as session:
            session.add(
                AppSetting(
                    key=self.key, value=json.dumps({"committed": False, "entries": self.entries})
                )
            )

    def mark_committed(self, session) -> None:
        # Commit this flag in the same transaction as deletion. Application
        # IDs may be reused later; recovery must never rely on ID existence.
        session.get(AppSetting, self.key).value = json.dumps(
            {"committed": True, "entries": self.entries}
        )

    def stage(self) -> None:
        for entry in self.entries:
            directory = Path(entry["directory"])
            if directory.exists():
                directory.rename(entry["staged"])

    def rollback(self) -> None:
        for entry in self.entries:
            staged, directory = Path(entry["staged"]), Path(entry["directory"])
            if staged.exists():
                if directory.exists():
                    raise OSError(
                        "Le dossier original existe déjà ; restauration automatique interrompue."
                    )
                staged.rename(directory)
        self._forget()

    def finish(self) -> bool:
        try:
            for entry in self.entries:
                staged = Path(entry["staged"])
                if staged.exists():
                    shutil.rmtree(staged)
            self._forget()
        except (OSError, SQLAlchemyError):
            # The durable plan retries cleanup on the next startup. Never
            # restore partially erased files after the database commit.
            return False
        return True

    def _forget(self) -> None:
        with write_session_scope(notify=False) as session:
            setting = session.get(AppSetting, self.key)
            if setting is not None:
                session.delete(setting)


def recover_application_deletions() -> None:
    from contextlib import ExitStack

    from smartapply.concurrency import TaskConflictError, application_publication
    from smartapply.logging_setup import get_logger

    with session_scope() as session:
        plans = [
            (setting.key, setting.value)
            for setting in session.scalars(
                select(AppSetting).where(AppSetting.key.startswith(DELETION_PREFIX))
            )
        ]
    for key, value in plans:
        try:
            plan = ApplicationOutputDeletion({})
            data = json.loads(value)
            plan.key, plan.entries = key, data["entries"]
            with ExitStack() as locks:
                for entry in plan.entries:
                    directory, staged = Path(entry["directory"]), Path(entry["staged"])
                    token = key.removeprefix(DELETION_PREFIX)
                    if (
                        not _owned_directory_name(directory.name, entry["application_id"])
                        or directory.resolve() != directory
                        or staged.parent != directory.parent
                        or staged.name != f".{directory.name}.deleting-{token}"
                        or staged.is_symlink()
                    ):
                        raise ValueError("Plan d’effacement de documents invalide.")
                for application_id in sorted({entry["application_id"] for entry in plan.entries}):
                    locks.enter_context(application_publication(0, application_id))
                if data["committed"]:
                    plan.finish()
                else:
                    plan.rollback()
        except (OSError, ValueError, KeyError, TypeError, TaskConflictError, SQLAlchemyError):
            # A locked or unavailable directory must not prevent app startup.
            get_logger(__name__).exception("Application document cleanup remains pending")
