"""Output directory helpers for generated application artifacts."""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from smartapply.logging_setup import get_logger
from smartapply.pipeline.output_publication import (
    ApplicationPublicationJournal,
    recover_application_publication,
)

if TYPE_CHECKING:
    from smartapply.pipeline.reports import ApplyReport

logger = get_logger(__name__)


_REPORT_PATH_FIELDS = (
    "docx_path",
    "cv_html_path",
    "cv_pdf_path",
    "letter_html_path",
    "letter_pdf_path",
)


def _filename_component(value: str, fallback: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    value = re.sub(r'[<>:"/\\|?*]', "-", value)
    return " ".join(value.split()).strip(" .-") or fallback


def application_document_stems(candidate_name: str, cv_title: str) -> tuple[str, str]:
    """Readable, portable names using the CV title and the candidate's chosen name."""
    name = _filename_component(candidate_name, "Candidate")
    title = _filename_component(cv_title, "CV")
    descriptor = f"{name} - {title}"
    # Reserve room for the longest prefix and extension. Both documents retain
    # the same descriptor, even with long titles or multibyte Unicode characters.
    budget = 255 - len(b"Motivation - ") - len(".docx")
    descriptor = descriptor.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    descriptor = descriptor.rstrip(" .-")
    return f"CV - {descriptor}", f"Motivation - {descriptor}"


def application_output_dir(output_dir: Path, application_id: int | None) -> Path:
    """Return the artifact directory for one persisted application."""
    if application_id is None:
        raise ValueError("application_id is required before rendering artifacts")
    return output_dir / str(int(application_id))


class AtomicApplicationOutput:
    """Stage a complete application directory and publish it atomically.

    A previous directory is kept as a temporary backup until database
    persistence succeeds. ``rollback`` restores it when any later step fails.
    """

    def __init__(
        self,
        output_dir: Path,
        application_id: int | None,
        *,
        final_dir: Path | None = None,
    ) -> None:
        # During regeneration the persisted artifact location is the source
        # of truth, even if the configured output directory has changed.
        self.final_dir = (
            Path(final_dir).expanduser().resolve()
            if final_dir is not None
            else application_output_dir(output_dir, application_id).expanduser().resolve()
        )
        self.final_dir.parent.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        self.staging_dir = self.final_dir.parent / f".{self.final_dir.name}.staging-{token}"
        self.backup_dir = self.final_dir.parent / f".{self.final_dir.name}.backup-{token}"
        self._published = False
        self._committed = False
        # The caller holds application_publication. A prior interrupted swap
        # must be reconciled from its SQLite decision before a new one starts.
        recover_application_publication(int(application_id))
        if not self.final_dir.exists() and list(
            self.final_dir.parent.glob(f".{self.final_dir.name}.backup-*")
        ):
            raise RuntimeError("An older application backup needs manual recovery")
        self._journal = ApplicationPublicationJournal(
            int(application_id), self.final_dir, self.staging_dir, self.backup_dir, token,
        )
        self.staging_dir.mkdir(parents=False, exist_ok=False)

    def _path_map(self, report: ApplyReport) -> dict[str, Path]:
        path_map: dict[str, Path] = {}
        for field_name in _REPORT_PATH_FIELDS:
            raw_path = getattr(report, field_name)
            if not raw_path:
                continue
            staged_path = Path(raw_path)
            try:
                relative_path = staged_path.relative_to(self.staging_dir)
            except ValueError as exc:
                raise ValueError(
                    f"Artifact {field_name} was written outside the staging directory"
                ) from exc
            if relative_path.name != str(relative_path):
                raise ValueError(f"Artifact {field_name} has an unsafe relative path")
            if not staged_path.is_file() or staged_path.stat().st_size <= 0:
                raise RuntimeError(f"Artifact {field_name} is missing or empty")
            path_map[field_name] = relative_path
        if not path_map:
            raise RuntimeError("No application artifact was generated")
        return path_map

    def prepare(self, report: ApplyReport) -> None:
        """Commit the recovery intent before entering the publication transaction."""
        if self._journal.prepared:
            raise RuntimeError("Application publication is already prepared")
        paths = self._path_map(report)
        self._journal.prepare([str(path) for path in paths.values()])

    def publish(self, report: ApplyReport) -> None:
        """Validate staged files, swap directories, and expose final paths."""
        if not self._journal.prepared:
            raise RuntimeError("Application publication was not prepared")
        path_map = self._path_map(report)

        try:
            if self.final_dir.exists():
                self.final_dir.replace(self.backup_dir)
            self.staging_dir.replace(self.final_dir)
            self._published = True
        except Exception:
            if not self.final_dir.exists() and self.backup_dir.exists():
                self.backup_dir.replace(self.final_dir)
            raise

        for field_name, relative_path in path_map.items():
            setattr(report, field_name, str(self.final_dir / relative_path))

    def mark_committed(self, session) -> None:
        self._journal.mark_committed(session)

    def commit(self) -> None:
        """Keep the published directory and discard its temporary backup."""
        if not self._published:
            raise RuntimeError("Cannot commit application artifacts before publish")
        self._committed = True
        try:
            self._journal.recover()
        except Exception:
            # DB publication already committed. Retain the journal and backup
            # for the next startup rather than reporting a false dossier failure.
            logger.exception("Application publication cleanup remains pending")

    def rollback(self) -> None:
        """Remove staged output and restore the previous complete directory."""
        if self._committed:
            return
        if self._journal.prepared:
            self._journal.recover()
            return
        if self._published:
            _remove_tree(self.final_dir)
        _remove_tree(self.staging_dir)
        if self.backup_dir.exists() and not self.final_dir.exists():
            self.backup_dir.replace(self.final_dir)


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
