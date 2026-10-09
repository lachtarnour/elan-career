"""Tests for the database module — models, session, repository."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each test gets a fresh SQLite file."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    # Force settings reload
    from smartapply.config import get_settings

    get_settings.cache_clear()
    from smartapply.database.session import reset_engine_cache

    reset_engine_cache()

    from smartapply.database.session import init_db

    init_db()
    yield
    reset_engine_cache()
    get_settings.cache_clear()


def test_init_creates_all_tables() -> None:
    from sqlalchemy import inspect

    from smartapply.database.session import get_engine

    insp = inspect(get_engine())
    tables = set(insp.get_table_names())
    expected = {
        "app_settings",
        "jobs",
        "job_scores",
        "job_analyses",
        "applications",
        "generated_documents",
        "llm_cache",
        "llm_usage",
    }
    assert expected.issubset(tables)
    assert "job_embeddings" not in tables
    assert "embedding_cache" not in tables
    assert "final_score" not in {column["name"] for column in insp.get_columns("job_scores")}


def test_match_score_migration_keeps_filter_evidence(tmp_path) -> None:
    from sqlalchemy import create_engine, text

    from smartapply.database.match_score_migration import remove_match_score

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE job_scores (id INTEGER PRIMARY KEY, job_id INTEGER, "
            "final_score FLOAT, semantic_score FLOAT, components JSON, computed_at DATETIME)"
        ))
        connection.execute(text("CREATE INDEX ix_job_scores_final_score ON job_scores (final_score)"))
        connection.execute(text("CREATE TABLE job_embeddings (id INTEGER PRIMARY KEY, job_id INTEGER)"))
        connection.execute(text(
            "INSERT INTO job_scores (job_id, final_score, semantic_score, components) "
            "VALUES (1, 0.8, 0.7, :components)"
        ), {"components": '{"final":0.8,"semantic":0.7,"filter_disposition":"rejected"}'})

    remove_match_score(engine)
    remove_match_score(engine)
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(job_scores)"))}
        assert columns == {"id", "job_id", "components", "computed_at"}
        assert connection.scalar(text("SELECT components FROM job_scores")) == (
            '{"filter_disposition":"rejected"}'
        )
        assert connection.scalar(text(
            "SELECT count(*) FROM sqlite_master WHERE name='job_embeddings'"
        )) == 0


def test_sqlite_is_tuned_for_concurrent_desktop_workloads() -> None:
    from sqlalchemy import inspect, text

    from smartapply.database.session import get_engine

    with get_engine().connect() as connection:
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        synchronous = connection.execute(text("PRAGMA synchronous")).scalar_one()
        cache_size = connection.execute(text("PRAGMA cache_size")).scalar_one()

    assert foreign_keys == 1
    assert busy_timeout == 30_000
    assert str(journal_mode).lower() == "wal"
    assert synchronous == 1
    assert cache_size == -32_768

    job_indexes = {item["name"] for item in inspect(get_engine()).get_indexes("jobs")}
    application_indexes = {
        item["name"] for item in inspect(get_engine()).get_indexes("applications")
    }
    assert "ix_jobs_scraped_at" in job_indexes
    assert "ix_jobs_shortlisted_at" in job_indexes
    assert "ix_jobs_status_scraped_at" in job_indexes
    assert "ix_applications_updated_at" in application_indexes
    assert "ix_applications_status_updated_at" in application_indexes


def test_lifecycle_backfills_do_not_promote_analyzed_jobs() -> None:
    from datetime import datetime, timezone

    from smartapply.database import backfill_shortlisted_at, session_scope
    from smartapply.database.models import Job, JobStatus, ShortlistOrigin
    from smartapply.database.repository import upsert_job
    from smartapply.database.session import backfill_analyzed_statuses

    with session_scope() as session:
        shortlisted = upsert_job(
            session,
            external_id="backfill:shortlisted",
            title="Data Scientist",
            company="Acme",
            description="Python et machine learning",
            source="manual",
            status=JobStatus.SHORTLISTED,
        )
        analyzed = upsert_job(
            session,
            external_id="backfill:analyzed",
            title="ML Engineer",
            company="Beta",
            description="Python et PyTorch",
            source="manual",
            status=JobStatus.ANALYZED,
        )
        legacy_analyzed = upsert_job(
            session,
            external_id="backfill:legacy-analyzed",
            title="Legacy ML Engineer",
            company="Delta",
            description="Python et PyTorch",
            source="manual",
            status=JobStatus.SCRAPED,
        )
        legacy_analyzed.analyzed_at = datetime.now(timezone.utc)
        ignored = upsert_job(
            session,
            external_id="backfill:filtered",
            title="Data Analyst",
            company="Gamma",
            description="Python et SQL",
            source="manual",
            status=JobStatus.FILTERED,
        )
        ids = (shortlisted.id, analyzed.id, legacy_analyzed.id, ignored.id)

    assert backfill_shortlisted_at() == 1
    assert backfill_analyzed_statuses() == 1

    with session_scope() as session:
        rows = [session.get(Job, job_id) for job_id in ids]
        assert rows[0] is not None and rows[0].shortlisted_at is not None
        assert rows[0].shortlist_origin == ShortlistOrigin.AUTOMATIC
        assert rows[1] is not None and rows[1].shortlisted_at is None
        assert rows[1].shortlist_origin is None
        assert rows[2] is not None and rows[2].shortlisted_at is None
        assert rows[2].status == JobStatus.ANALYZED
        assert rows[3] is not None and rows[3].shortlisted_at is None


def test_unanalyzed_shortlist_markers_are_reconciled() -> None:
    from datetime import datetime, timezone

    from smartapply.database import clear_unanalyzed_shortlists, session_scope
    from smartapply.database.models import Job, JobStatus, ShortlistOrigin
    from smartapply.database.repository import upsert_job

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        filtered = upsert_job(
            session,
            external_id="repair:filtered-shortlist",
            title="Data Scientist",
            company="Acme",
            description="Python et machine learning",
            source="manual",
            status=JobStatus.FILTERED,
        )
        filtered.filtered_at = now
        filtered.shortlisted_at = now
        filtered.shortlist_origin = ShortlistOrigin.AUTOMATIC

        scraped = upsert_job(
            session,
            external_id="repair:scraped-shortlist",
            title="ML Engineer",
            company="Beta",
            description="Python et PyTorch",
            source="manual",
            status=JobStatus.SHORTLISTED,
        )
        scraped.shortlisted_at = now
        scraped.shortlist_origin = ShortlistOrigin.MANUAL

        analyzed = upsert_job(
            session,
            external_id="repair:valid-shortlist",
            title="Research Engineer",
            company="Gamma",
            description="Python et NLP",
            source="manual",
            status=JobStatus.SHORTLISTED,
        )
        analyzed.analyzed_at = now
        analyzed.shortlisted_at = now
        analyzed.shortlist_origin = ShortlistOrigin.AUTOMATIC
        ids = (filtered.id, scraped.id, analyzed.id)

    assert clear_unanalyzed_shortlists() == 2

    with session_scope() as session:
        filtered, scraped, analyzed = (session.get(Job, job_id) for job_id in ids)
        assert filtered is not None and filtered.shortlisted_at is None
        assert filtered.shortlist_origin is None
        assert filtered.status == JobStatus.FILTERED
        assert scraped is not None and scraped.shortlisted_at is None
        assert scraped.shortlist_origin is None
        assert scraped.status == JobStatus.SCRAPED
        assert analyzed is not None and analyzed.shortlisted_at is not None
        assert analyzed.shortlist_origin == ShortlistOrigin.AUTOMATIC


def test_upsert_job_inserts_then_updates() -> None:
    from smartapply.database import session_scope
    from smartapply.database.repository import upsert_job

    with session_scope() as s:
        job = upsert_job(
            s,
            external_id="serpapi:abc",
            title="Data Scientist",
            company="Acme",
            description="desc",
            source="serpapi",
        )
        first_id = job.id

    with session_scope() as s:
        job = upsert_job(
            s,
            external_id="serpapi:abc",
            title="Data Scientist Senior",
            description="updated",
        )
        assert job.id == first_id
        assert job.title == "Data Scientist Senior"
        assert job.company == "Acme"


def test_application_document_and_tracking_flow() -> None:
    from smartapply.database import session_scope
    from smartapply.database.repository import (
        add_document,
        create_or_get_application,
        update_application_tracking,
        upsert_job,
    )

    with session_scope() as s:
        job = upsert_job(
            s,
            external_id="manual:42",
            title="ML Engineer",
            company="Acme",
            description="desc",
            source="manual",
        )
        app = create_or_get_application(s, job.id)
        add_document(s, app.id, doc_type="cv_docx", path="/tmp/cv.docx")
        add_document(s, app.id, doc_type="motivation_letter", content="Bonjour,")

        # idempotent
        same = create_or_get_application(s, job.id)
        assert same.id == app.id
        assert len(app.documents) == 2

        tracked = update_application_tracking(
            s,
            app.id,
            status="sent",
            notes="Relancer dans une semaine.",
        )
        assert tracked.status == "sent"
        assert tracked.job.status == "sent"
        assert tracked.notes == "Relancer dans une semaine."


def test_audit_component_update_preserves_score_computation_time() -> None:
    from datetime import datetime

    from smartapply.database import session_scope
    from smartapply.database.repository import set_score, upsert_job

    original_time = datetime(2000, 1, 1)
    with session_scope() as session:
        job = upsert_job(
            session,
            external_id="score:audit-only",
            title="ML Engineer",
            company="Acme",
            description="Machine learning.",
            source="manual",
        )
        score = set_score(
            session,
            job.id,
            components={"manual_audit": True},
            computed_at=original_time,
        )
        session.flush()
        score = set_score(
            session,
            job.id,
            components={"manual_audit": True, "verified": True},
        )
        session.flush()

        assert score.computed_at == original_time


def test_usage_attribution_backfills_stable_reference_for_legacy_rows() -> None:
    from sqlalchemy import select

    from smartapply.database import backfill_usage_job_external_ids, session_scope
    from smartapply.database.models import LLMUsage
    from smartapply.database.repository import upsert_job

    with session_scope() as session:
        job = upsert_job(
            session,
            external_id="usage:stable-reference",
            title="ML Engineer",
            company="Acme",
            description="desc",
            source="manual",
        )
        session.flush()
        usage = LLMUsage(
            purpose="legacy",
            model="gpt-5.4",
            prompt_tokens=100,
            completion_tokens=10,
            cost_usd=0.001,
            job_id=job.id,
            job_external_id=None,
        )
        session.add(usage)

    assert backfill_usage_job_external_ids() == 1
    with session_scope() as session:
        usage = session.scalar(select(LLMUsage))
        assert usage is not None
        assert usage.job_external_id == "usage:stable-reference"


def test_auto_migrate_adds_missing_columns(tmp_path, monkeypatch) -> None:
    """When the model gains a column, init_db() should add it to existing DBs.

    We don't use SQLite ``DROP COLUMN`` (unsupported on some builds).
    Instead, we hand-craft a minimal pre-migration ``applications`` table
    and verify ``auto_migrate`` brings it up to spec.
    """
    db_path = tmp_path / "auto_migrate_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    from smartapply.config import get_settings

    get_settings.cache_clear()
    from smartapply.database.session import reset_engine_cache

    reset_engine_cache()

    from sqlalchemy import create_engine, inspect, text

    # 1. Manually craft a minimal "legacy" applications table that lacks the
    #    new columns added later in development.
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE applications (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    status VARCHAR(50) DEFAULT 'analyzed' NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
    engine.dispose()

    # 2. init_db() should call create_all() (no-op on the existing table)
    #    AND auto_migrate() (adds the missing columns).
    from smartapply.database import get_engine, init_db

    reset_engine_cache()
    init_db()

    # 3. Verify the new columns now exist
    insp = inspect(get_engine())
    cols = {c["name"] for c in insp.get_columns("applications")}
    for expected_col in (
        "form_submission_url",
        "form_submitted_at",
        "cv_docx_path",
        "cv_pdf_path",
        "cv_json",
        "validation_warnings",
        "notes",
    ):
        assert expected_col in cols, f"missing column after auto_migrate: {expected_col}"

    # 4. Re-running init_db is a no-op (idempotent)
    from smartapply.database.session import auto_migrate

    assert auto_migrate() == []

    reset_engine_cache()
    get_settings.cache_clear()


def test_rescue_archived_job_resets_state_without_fabricating_scores() -> None:
    """The macOS offers view lets the user override a wrong filter
    rejection by re-injecting the offer into the pending relevance queue.
    The repository helper must reset the archive markers, clear stale numeric
    scores and preserve a full ``components.manual_rescue`` audit block.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobScore, JobStatus
    from smartapply.database.repository import (
        mark_archived,
        rescue_archived_job,
        set_score,
        upsert_job,
    )

    # Arrange: an archived job carrying a rejection audit trail.
    with session_scope() as s:
        job = upsert_job(
            s,
            external_id="rescue:001",
            title="Data Scientist H/F",
            company="Acme",
            description="ML, Python, deep learning.",
            location="Paris",
            source="francetravail",
        )
        set_score(
            s,
            job.id,
            components={
                "rejection_stage": "local_filter",
                "rejection_reasons": [
                    "contract_visible_text:apprentissage",
                ],
                "rejection_summary": "contract_visible_text:apprentissage",
                "hard_reject": {"ruleset_version": "hard-reject-v2", "rejected": True},
            },
        )
        mark_archived(s, job.id)
        job_id = job.id

    # Act: rescue with a justification.
    with session_scope() as s:
        rescued = rescue_archived_job(
            s,
            job_id,
            justification="filtre trop strict sur 'apprentissage automatique'",
        )
        assert rescued is not None
        assert rescued.id == job_id

    # Assert: archive cleared and status FILTERED while the
    # audit block preserves the previous rejection reasons.
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.FILTERED
        assert job.archived_at is None
        assert job.analyzed_at is None
        assert job.filtered_at is not None
        assert job.ranked_at is None
        assert job.shortlisted_at is None
        # SQLite drops the tz on read — coerce to UTC before subtracting.
        filtered_at = job.filtered_at
        if filtered_at.tzinfo is None:
            filtered_at = filtered_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - filtered_at
        assert 0 <= delta.total_seconds() < 60

        score = s.execute(select(JobScore).where(JobScore.job_id == job_id)).scalar_one()
        comps = score.components
        assert comps["filter_disposition"] == "pending_relevance"
        assert "hard_reject" not in comps
        rescue = comps["manual_rescue"]
        assert rescue["schema_version"] == 3
        assert rescue["justification"].startswith("filtre trop strict")
        assert rescue["previous_hard_reject"]["rejected"] is True
        assert rescue["previous_rejection"]["stage"] == "local_filter"
        assert "contract_visible_text:apprentissage" in (
            rescue["previous_rejection"]["reasons"] or []
        )
