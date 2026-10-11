"""The physical SQLite rename must preserve all existing rows and identities."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select

from smartapply.database import get_engine, init_db, session_scope, write_session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.models import (
    Application,
    DuplicateReviewEvent,
    GeneratedDocument,
    Job,
    JobAnalysis,
    JobScore,
    LLMCache,
    LLMUsage,
)
from smartapply.database.session import reset_engine_cache
from smartapply.database.source_company_migration import SourceCompanyMigrationError
from smartapply.pipeline.snapshots import job_snapshot_key


def _rows(path):
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        return {
            table: list(connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
            for table in tables
        }


def test_fresh_database_uses_physical_source_company(isolated_db):
    columns = {column["name"] for column in inspect(get_engine()).get_columns("jobs")}
    assert "source_company" in columns
    assert "company" not in columns
    assert "ix_jobs_company" in {
        index["name"] for index in inspect(get_engine()).get_indexes("jobs")
    }


def test_legacy_rename_preserves_rows_relations_snapshots_and_indexes(isolated_db):
    path = isolated_db / "test.db"
    now = datetime.now(timezone.utc)
    with write_session_scope() as session:
        first = Job(external_id="legacy-1", title="ML engineer", source_company="Publisher",
                    source="fixture", description="An offer", status="analyzed")
        second = Job(external_id="legacy-2", title="ML engineer", source_company="Employer",
                     source="fixture", description="Another offer", canonical_job_id=None)
        session.add_all((first, second))
        session.flush()
        second.canonical_job_id = first.id
        first.score = JobScore(components={"final": 0.8})
        first.analysis = JobAnalysis(raw_response={"extracted_company_name": "Employer"},
                                     fit_score=0.8, model_used="saved")
        application = Application(job_id=first.id, status="ready_for_form_submission",
                                  cv_json={"saved": True}, cv_docx_path="old.docx")
        session.add(application)
        session.flush()
        session.add_all((
            GeneratedDocument(application_id=application.id, doc_type="motivation_letter_pdf",
                              path="old.pdf", content="historical"),
            LLMCache(cache_key="a" * 64, model="saved", response='{"ok":true}',
                     created_at=now),
            LLMUsage(purpose="job_offer_extraction", model="saved", job_id=first.id,
                     job_external_id=first.external_id, cache_key="a" * 64),
            DuplicateReviewEvent(job_id=second.id, reference_id=first.id,
                                 policy_version="v1", action="confirmed", reason="source",
                                 before_state={}, after_state={}, history_snapshot={}),
        ))
        run = AnalysisBatchRun(id="saved-run", state="completed", model="saved",
                               provider="mock", profile_data={"saved": True})
        session.add(run)
        session.flush()
        session.add(AnalysisBatchItem(run_id=run.id, job_id=second.id,
                                      job_snapshot=job_snapshot_key(second), title=second.title,
                                      company=second.source_company, analyzer_input={"company": "Employer"}))
        session.flush()
        expected_snapshot = job_snapshot_key(first)

    reset_engine_cache()
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE "jobs" RENAME COLUMN "source_company" TO "company"')
    original = _rows(path)
    with sqlite3.connect(path) as connection:
        old_indexes = {row[1] for row in connection.execute('PRAGMA index_list("jobs")')}

    init_db(run_maintenance=False)
    migrated = _rows(path)
    assert migrated == original
    backups = list(isolated_db.glob("test.db.pre-source-company-*.bak"))
    assert len(backups) == 1
    assert _rows(backups[0]) == original
    with sqlite3.connect(path) as connection:
        assert {row[1] for row in connection.execute('PRAGMA index_list("jobs")')} == old_indexes
        assert [row[2] for row in connection.execute('PRAGMA index_info("ix_jobs_company")')] == [
            "source_company"
        ]
        assert not list(connection.execute("PRAGMA foreign_key_check"))
        assert {row[1] for row in connection.execute('PRAGMA table_info("jobs")')} >= {"source_company"}
    with session_scope() as session:
        job = session.scalar(select(Job).where(Job.external_id == "legacy-1"))
        assert job.source_company == "Publisher"
        assert job_snapshot_key(job) == expected_snapshot
        assert job.analysis.raw_response["extracted_company_name"] == "Employer"
        assert job.application.documents[0].path == "old.pdf"

    init_db(run_maintenance=False)
    assert _rows(path) == migrated
    assert len(list(isolated_db.glob("test.db.pre-source-company-*.bak"))) == 1


def test_ambiguous_schema_fails_without_mutating_data(isolated_db):
    path = isolated_db / "test.db"
    reset_engine_cache()
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE "jobs" ADD COLUMN "company" VARCHAR(255)')
        connection.execute(
            "INSERT INTO jobs(external_id,title,source_company,company,description,source,status,scraped_at) "
            "VALUES('conflict','Engineer','Original','Different','Offer','fixture','scraped','2026-01-01')"
        )
    before = _rows(path)
    with pytest.raises(SourceCompanyMigrationError):
        init_db(run_maintenance=False)
    assert _rows(path) == before
    assert not list(isolated_db.glob("test.db.pre-source-company-*.bak"))


def test_failed_rename_rolls_back_and_keeps_recovery_backup(isolated_db):
    path = isolated_db / "test.db"
    reset_engine_cache()
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE "jobs" RENAME COLUMN "source_company" TO "company"')
        connection.execute('CREATE VIEW broken_legacy_view AS SELECT * FROM missing_table')
    before = _rows(path)

    with pytest.raises(sqlite3.OperationalError):
        init_db(run_maintenance=False)

    assert _rows(path) == before
    with sqlite3.connect(path) as connection:
        assert "company" in {row[1] for row in connection.execute('PRAGMA table_info("jobs")')}
    backups = list(isolated_db.glob("test.db.pre-source-company-*.bak"))
    assert len(backups) == 1
    assert _rows(backups[0]) == before
