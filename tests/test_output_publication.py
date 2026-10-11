"""Crash recovery must keep SQLite's document version and the files together."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from smartapply.config import get_settings
from smartapply.database import init_db, session_scope, write_session_scope
from smartapply.database.models import Application, AppSetting, GeneratedDocument
from smartapply.desktop.offer_deletion import delete_ready_offer
from smartapply.offers import ManualOfferInput
from smartapply.pipeline import Pipeline
from smartapply.pipeline.applier import Applier
from smartapply.pipeline.output_paths import AtomicApplicationOutput
from smartapply.pipeline.output_publication import (
    PUBLICATION_PREFIX,
    recover_application_publications,
)
from tests.test_pipeline import _register_llm_responses


@pytest.fixture
def analyzed_job(isolated_db):
    _register_llm_responses()
    pipeline = Pipeline()
    ingested = pipeline.ingest_manual_offer(ManualOfferInput(
        title="Data Scientist NLP", company="Acme AI", location="Paris",
        description="Build RAG pipelines with Python and PyTorch.",
        application_url="https://acme.example/job",
    ))
    job_id = ingested.job_ids[0]
    pipeline.prepare_analysis(job_ids=[job_id], exclude_applications=False)
    assert pipeline.analyze_jobs([job_id], exclude_applications=False).analyzed == 1
    return job_id


def _previous_version(job_id: int, *, artifact_dir: Path | None = None) -> tuple[int, Path, Path]:
    report = Pipeline().apply_to(job_id, artifact_dir=artifact_dir)
    old_file = Path(report.docx_path)
    previous = old_file.with_name("previous-cv.docx")
    old_file.rename(previous)
    with write_session_scope() as session:
        application = session.get(Application, report.application_id)
        application.cv_docx_path = str(previous)
        document = session.scalar(select(GeneratedDocument).where(
            GeneratedDocument.application_id == application.id,
            GeneratedDocument.doc_type == "cv_docx",
        ))
        document.path = str(previous)
    return report.application_id, previous, old_file


def _pending() -> list[AppSetting]:
    with session_scope() as session:
        return list(session.scalars(select(AppSetting).where(
            AppSetting.key.startswith(PUBLICATION_PREFIX)
        )))


def _crash_during_regeneration(
    job_id: int, stage: str, *, artifact_dir: Path | None = None,
) -> None:
    pid = os.fork()
    if pid == 0:
        from smartapply.database.session import reset_engine_cache

        reset_engine_cache()
        if stage == "prepared":
            AtomicApplicationOutput.publish = lambda self, report: os._exit(71)
        elif stage == "backed_up":
            original_replace = Path.replace

            def crash_after_backup(self, target):
                result = original_replace(self, target)
                if ".backup-" in Path(target).name and self.name.isdigit():
                    os._exit(72)
                return result

            Path.replace = crash_after_backup
        elif stage == "published":
            Applier._persist_application = lambda self, **kwargs: os._exit(73)
        elif stage == "committed":
            AtomicApplicationOutput.commit = lambda self: os._exit(74)
        try:
            Pipeline().apply_to(job_id, force_regenerate=True, artifact_dir=artifact_dir)
        except BaseException:
            os._exit(90)
        os._exit(91)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == {
        "prepared": 71, "backed_up": 72, "published": 73, "committed": 74,
    }[stage]


@pytest.mark.parametrize("stage", ("prepared", "backed_up", "published", "committed"))
def test_crash_recovery_follows_sqlite_commit(analyzed_job, stage):
    app_id, previous, new_name = _previous_version(analyzed_job)
    _crash_during_regeneration(analyzed_job, stage)
    assert len(_pending()) == 1
    # Startup performs recovery, including when the final directory is absent.
    init_db(run_maintenance=False)
    with session_scope() as session:
        stored = session.get(Application, app_id).cv_docx_path
        persisted_doc = session.scalar(select(GeneratedDocument).where(
            GeneratedDocument.application_id == app_id,
            GeneratedDocument.doc_type == "cv_docx",
        )).path
    expected = str(new_name if stage == "committed" else previous)
    assert stored == expected == persisted_doc
    assert Path(expected).is_file()
    assert not (new_name if stage != "committed" else previous).exists()
    assert not _pending()
    assert not list(previous.parent.parent.glob(f".{app_id}.backup-*"))
    assert not list(previous.parent.parent.glob(f".{app_id}.staging-*"))
    # Recovery is safe if startup is repeated.
    init_db(run_maintenance=False)
    assert Path(expected).is_file()


def test_recovery_interrupted_after_restoring_old_files_is_repeatable(analyzed_job):
    _, previous, _ = _previous_version(analyzed_job)
    _crash_during_regeneration(analyzed_job, "published")
    assert not previous.exists()
    pid = os.fork()
    if pid == 0:
        from smartapply.database.session import reset_engine_cache

        reset_engine_cache()
        original_replace = Path.replace

        def crash_after_restore(self, target):
            result = original_replace(self, target)
            if ".backup-" in self.name and Path(target).name.isdigit():
                os._exit(75)
            return result

        Path.replace = crash_after_restore
        recover_application_publications()
        os._exit(90)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 75
    assert previous.is_file() and len(_pending()) == 1
    recover_application_publications()
    assert previous.is_file() and not _pending()


def test_unknown_final_version_does_not_delete_backup(analyzed_job):
    app_id, previous, _ = _previous_version(analyzed_job)
    _crash_during_regeneration(analyzed_job, "published")
    final_dir = get_settings().output_dir / str(app_id)
    marker = next(final_dir.glob(".elan-publication-*"))
    marker.unlink()
    backup = next(final_dir.parent.glob(f".{app_id}.backup-*"))
    recover_application_publications()
    assert backup.is_dir() and (backup / previous.name).is_file()
    assert final_dir.is_dir() and len(_pending()) == 1
    with pytest.raises(RuntimeError, match="identify"):
        AtomicApplicationOutput(get_settings().output_dir, app_id)
    assert backup.is_dir() and len(_pending()) == 1


def test_first_generation_crash_does_not_publish_uncommitted_files(analyzed_job):
    _crash_during_regeneration(analyzed_job, "published")
    with session_scope() as session:
        application = session.scalar(select(Application).where(Application.job_id == analyzed_job))
        app_id = application.id
        assert application.cv_json is None and not application.documents
    assert (get_settings().output_dir / str(app_id)).is_dir()
    init_db(run_maintenance=False)
    assert not (get_settings().output_dir / str(app_id)).exists()
    assert not _pending()


def test_deletion_resolves_a_committed_publication_before_erasing(analyzed_job, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(AtomicApplicationOutput, "commit", lambda self: None)
        report = Pipeline().apply_to(analyzed_job)
    assert len(_pending()) == 1
    result = delete_ready_offer(analyzed_job)
    assert result.files_removed and not _pending()
    assert not (get_settings().output_dir / str(report.application_id)).exists()


def test_unknown_legacy_backup_is_preserved(analyzed_job):
    report = Pipeline().apply_to(analyzed_job)
    final = Path(report.docx_path).parent
    legacy = final.parent / f".{final.name}.backup-{'f' * 32}"
    legacy.mkdir()
    (legacy / "old.docx").write_bytes(b"old-version")
    artifacts = AtomicApplicationOutput(get_settings().output_dir, report.application_id)
    artifacts.rollback()
    assert (legacy / "old.docx").read_bytes() == b"old-version"
    assert final.is_dir()


def test_recovery_keeps_legacy_application_directory(analyzed_job, tmp_path):
    legacy = tmp_path / "previous-output" / "application-42"
    app_id, previous, new_name = _previous_version(analyzed_job, artifact_dir=legacy)
    _crash_during_regeneration(analyzed_job, "published", artifact_dir=legacy)
    init_db(run_maintenance=False)
    with session_scope() as session:
        assert session.get(Application, app_id).cv_docx_path == str(previous)
    assert previous.is_file() and not new_name.exists()
    assert not _pending()


def test_relative_output_directory_publishes_and_recovers_after_cwd_change(
    analyzed_job, monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OUTPUT_DIR", "relative-documents")
    get_settings.cache_clear()
    # Simulate interrupted cleanup after the database commit, leaving a
    # journal that must remain valid in a different working directory.
    with monkeypatch.context() as patch:
        patch.setattr(AtomicApplicationOutput, "commit", lambda self: None)
        report = Pipeline().apply_to(analyzed_job)
    expected_dir = tmp_path / "relative-documents" / str(report.application_id)
    assert Path(report.docx_path).parent == expected_dir
    assert len(_pending()) == 1
    with session_scope() as session:
        app = session.get(Application, report.application_id)
        assert app.documents
        paths = [Path(document.path) for document in app.documents if document.path]
        assert paths and all(path.parent == expected_dir for path in paths)
        assert all(path.is_absolute() and path.is_file() for path in paths)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    recover_application_publications()
    assert not _pending() and Path(report.docx_path).is_file()
    regenerated = Pipeline().apply_to(
        analyzed_job, force_regenerate=True, artifact_dir=expected_dir,
    )
    assert Path(regenerated.docx_path).parent == expected_dir
    assert not _pending()
