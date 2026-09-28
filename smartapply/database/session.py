"""Engine and Session management."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Column, create_engine, event, func, inspect, or_, select, text, update
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex

from smartapply.config import get_settings
from smartapply.database.models import (
    Base,
    Job,
    JobDuplicateStatus,
    JobScore,
    JobStatus,
    LLMUsage,
    ShortlistOrigin,
)
from smartapply.database.repository.applications import clear_shortlist_for_sent_applications
from smartapply.database.repository.llm_cache import purge_expired_cache
from smartapply.logging_setup import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    if is_sqlite:
        _configure_sqlite(engine)
    return engine


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA cache_size=-32768")
        finally:
            cursor.close()


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=True)


def init_db() -> None:
    """Create all tables, then add any new columns the models gained.

    The auto-migration step (``auto_migrate``) handles the dev workflow
    where the user added fields to a model after the DB was first created.
    It only does ``ALTER TABLE ADD COLUMN`` — drops/renames/type changes
    still require Alembic.
    """
    Base.metadata.create_all(get_engine())
    auto_migrate()
    ensure_indexes()
    backfill_shortlisted_at()
    clear_archived_shortlists()
    clear_unanalyzed_shortlists()
    backfill_analyzed_statuses()
    backfill_usage_job_external_ids()
    backfill_duplicate_reviews()
    with session_scope() as session:
        clear_shortlist_for_sent_applications(session)
        purge_expired_cache(
            session,
            ttl_days=get_settings().llm_cache_ttl_days,
        )


def drop_db() -> None:
    """Drop all tables — destructive, use only in tests."""
    Base.metadata.drop_all(get_engine())


def auto_migrate() -> list[str]:
    """Add columns present in the models but missing in the DB.

    Returns the list of SQL statements that were executed so callers can log
    them. Safe to run on every startup — when models and DB agree, it's a
    no-op single inspect() call.
    """
    engine = get_engine()
    inspector = inspect(engine)
    executed: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            if not inspector.has_table(table.name):
                continue  # create_all just made it; no ALTER needed
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                statement = _build_add_column_sql(table.name, column, engine.dialect)
                logger.info("Auto-migrate: %s", statement)
                conn.execute(text(statement))
                executed.append(statement)

    return executed


def ensure_indexes() -> None:
    """Create model indexes added after an existing SQLite database was created."""
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            for table in Base.metadata.sorted_tables:
                for index in table.indexes:
                    connection.execute(CreateIndex(index, if_not_exists=True))
        return
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)


def backfill_usage_job_external_ids() -> int:
    """Preserve attribution for legacy usage rows while their jobs still exist."""
    job_external_id = select(Job.external_id).where(Job.id == LLMUsage.job_id).scalar_subquery()
    with get_engine().begin() as conn:
        result = conn.execute(
            update(LLMUsage)
            .where(
                LLMUsage.job_id.is_not(None),
                LLMUsage.job_external_id.is_(None),
            )
            .values(job_external_id=job_external_id)
        )
    return int(result.rowcount or 0)


def backfill_duplicate_reviews() -> int:
    """Hold legacy fuzzy matches for review without merging or deleting rows.

    New ingests perform the same check before persistence.  This one-time-safe
    pass protects databases created before duplicate review existed, including
    rows that already own an archived or active application.
    """
    from smartapply.dedup import Deduplicator

    with session_scope() as session:
        jobs = session.execute(select(Job).order_by(Job.id.asc())).scalars().all()
        matcher = Deduplicator()
        updated = 0
        for index, job in enumerate(jobs):
            review_status = job.duplicate_review_status or JobDuplicateStatus.NONE
            candidate = matcher.find_probable_duplicate(job, jobs[:index])
            if candidate is None or int(candidate.job.id) == int(job.id):
                continue

            if review_status != JobDuplicateStatus.NONE:
                continue
            candidate_archived = bool(
                candidate.job.archived_at or candidate.job.status == JobStatus.ARCHIVED
            )
            job_archived = bool(job.archived_at or job.status == JobStatus.ARCHIVED)
            if job_archived and candidate_archived:
                # A pair with no actionable offer does not need a human
                # decision. Record the skip so this legacy repair remains
                # idempotent on future startups.
                job.possible_duplicate_of_id = None
                job.duplicate_review_status = JobDuplicateStatus.REJECTED
                job.duplicate_match_type = candidate.match_type
                job.duplicate_confidence = candidate.confidence
                job.status = JobStatus.ARCHIVED
                updated += 1
                continue
            job.possible_duplicate_of_id = int(candidate.job.id)
            job.duplicate_review_status = JobDuplicateStatus.PENDING
            job.duplicate_match_type = candidate.match_type
            job.duplicate_confidence = candidate.confidence
            # An old shortlist marker must not remain actionable while the
            # user is deciding.  Existing analysis/application history stays
            # intact and is still used by the application guard.
            job.shortlisted_at = None
            job.shortlist_origin = None
            updated += 1
        return updated


def backfill_shortlisted_at() -> int:
    """Preserve the Top selection for databases created before the dedicated marker."""
    with get_engine().begin() as conn:
        result = conn.execute(
            update(Job)
            .where(
                Job.shortlisted_at.is_(None),
                Job.archived_at.is_(None),
                Job.status == JobStatus.SHORTLISTED,
            )
            .values(
                shortlisted_at=func.coalesce(
                    Job.ranked_at,
                    Job.analyzed_at,
                    Job.scraped_at,
                ),
            )
        )
        rows = conn.execute(
            select(Job.id, JobScore.components)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .where(
                Job.shortlisted_at.is_not(None),
                Job.shortlist_origin.is_(None),
            )
        ).all()
        for job_id, components in rows:
            manual_state = (components or {}).get("manual_shortlist")
            is_manual = isinstance(manual_state, dict) and manual_state.get("selected") is True
            conn.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    shortlist_origin=(
                        ShortlistOrigin.MANUAL if is_manual else ShortlistOrigin.AUTOMATIC
                    )
                )
            )
    return int(result.rowcount or 0)


def backfill_analyzed_statuses() -> int:
    """Align legacy status labels with completed analysis markers."""
    with get_engine().begin() as conn:
        result = conn.execute(
            update(Job)
            .where(
                Job.status.in_((JobStatus.SCRAPED, JobStatus.FILTERED)),
                Job.analyzed_at.is_not(None),
                Job.shortlisted_at.is_(None),
                Job.archived_at.is_(None),
            )
            .values(status=JobStatus.ANALYZED)
        )
    return int(result.rowcount or 0)


def clear_archived_shortlists() -> int:
    """Remove stale Top-selection markers from legacy archived offers."""
    with get_engine().begin() as conn:
        result = conn.execute(
            update(Job)
            .where(
                Job.archived_at.is_not(None),
                or_(
                    Job.shortlisted_at.is_not(None),
                    Job.shortlist_origin.is_not(None),
                    Job.status == JobStatus.SHORTLISTED,
                ),
            )
            .values(
                shortlisted_at=None,
                shortlist_origin=None,
                status=JobStatus.ARCHIVED,
            )
        )
    return int(result.rowcount or 0)


def clear_unanalyzed_shortlists() -> int:
    """Remove invalid Top-selection markers from unanalyzed offers.

    The final Top selection is only valid after LLM analysis. This repairs
    legacy databases that persisted ``shortlisted`` before that invariant was
    enforced, while keeping the offer available in the pending queue.
    """
    base_conditions = (
        or_(
            Job.shortlisted_at.is_not(None),
            Job.shortlist_origin.is_not(None),
            Job.status == JobStatus.SHORTLISTED,
        ),
        Job.analyzed_at.is_(None),
        Job.archived_at.is_(None),
    )
    with get_engine().begin() as conn:
        filtered = conn.execute(
            update(Job)
            .where(*base_conditions, Job.filtered_at.is_not(None))
            .values(
                shortlisted_at=None,
                shortlist_origin=None,
                status=JobStatus.FILTERED,
            )
        )
        scraped = conn.execute(
            update(Job)
            .where(*base_conditions, Job.filtered_at.is_(None))
            .values(
                shortlisted_at=None,
                shortlist_origin=None,
                status=JobStatus.SCRAPED,
            )
        )
    return int(filtered.rowcount or 0) + int(scraped.rowcount or 0)


def _build_add_column_sql(table_name: str, column: Column, dialect: Dialect) -> str:
    """Render ``ALTER TABLE … ADD COLUMN …`` from a SQLAlchemy Column."""
    type_sql = column.type.compile(dialect=dialect)
    parts = [f"ALTER TABLE {table_name} ADD COLUMN {column.name} {type_sql}"]

    default_literal = _python_default_to_sql_literal(column)
    if default_literal is not None:
        parts.append(f"DEFAULT {default_literal}")

    if not column.nullable:
        # SQLite requires either a DEFAULT or the column to be nullable
        # when adding to a non-empty table. We've already added DEFAULT above
        # when the model provided one.
        parts.append("NOT NULL")

    return " ".join(parts)


def _python_default_to_sql_literal(column: Column) -> str | None:
    """Convert the model's Python default to a SQL literal."""
    default = column.default
    if default is None:
        return None
    value = getattr(default, "arg", None)
    if callable(value) or value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Clear cached engine/session — useful in tests that switch DB urls."""
    get_engine.cache_clear()
    get_session_factory.cache_clear()
