"""Output directory helpers for generated application artifacts."""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from smartapply.pipeline.reports import ApplyReport


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


def application_document_stems(candidate_name: str, job_title: str) -> tuple[str, str]:
    """Readable, portable names using the offer title and the candidate's chosen name."""
    name = _filename_component(candidate_name, "Candidate")
    title = _filename_component(job_title, "Poste")
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
            else application_output_dir(output_dir, application_id)
        )
        self.final_dir.parent.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        self.staging_dir = self.final_dir.parent / f".{self.final_dir.name}.staging-{token}"
        self.backup_dir = self.final_dir.parent / f".{self.final_dir.name}.backup-{token}"
        self._published = False
        self._committed = False
        self._recover_interrupted_publish()
        self.staging_dir.mkdir(parents=False, exist_ok=False)

    def _recover_interrupted_publish(self) -> None:
        """Restore a backup left by a crash before the new directory appeared."""
        backups = sorted(
            self.final_dir.parent.glob(f".{self.final_dir.name}.backup-*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not self.final_dir.exists() and backups:
            backups[0].replace(self.final_dir)
            backups = backups[1:]
        for stale in backups:
            _remove_tree(stale)
        for stale in self.final_dir.parent.glob(f".{self.final_dir.name}.staging-*"):
            _remove_tree(stale)

    def publish(self, report: ApplyReport) -> None:
        """Validate staged files, swap directories, and expose final paths."""
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
            if not staged_path.is_file() or staged_path.stat().st_size <= 0:
                raise RuntimeError(f"Artifact {field_name} is missing or empty")
            path_map[field_name] = relative_path

        if not path_map:
            raise RuntimeError("No application artifact was generated")

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

    def commit(self) -> None:
        """Keep the published directory and discard its temporary backup."""
        if not self._published:
            raise RuntimeError("Cannot commit application artifacts before publish")
        self._committed = True
        _remove_tree(self.backup_dir)

    def rollback(self) -> None:
        """Remove staged output and restore the previous complete directory."""
        if self._committed:
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
