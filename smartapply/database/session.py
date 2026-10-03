"""Engine and Session management."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import lru_cache
from threading import RLock

from sqlalchemy import Column, create_engine, event, func, inspect, or_, select, text, update
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex

from smartapply.config import get_settings
from smartapply.database.models import (
    AppSetting,
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
_write_lock = RLock()
_observer_lock = RLock()
_database_observers: dict[object, tuple[Callable[..., None], bool]] = {}
_MAINTENANCE_KEY = "desktop_startup_maintenance"
_MAINTENANCE_VERSION = "2"


class StartupMaintenanceRequiredError(RuntimeError):
    def __init__(self):
        super().__init__("La vérification initiale des doublons est en cours. Cette action attend sa fin.")


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


def init_db(*, run_maintenance: bool = True) -> None:
    """Create all tables, then add any new columns the models gained.

    The auto-migration step (``auto_migrate``) handles the dev workflow
    where the user added fields to a model after the DB was first created.
    It only does ``ALTER TABLE ADD COLUMN`` — drops/renames/type changes
    still require Alembic.
    """
    Base.metadata.create_all(get_engine())
    auto_migrate()
    ensure_indexes()
    # Retention is periodic housekeeping, not a historical schema repair.
    # Provider calls also purge expired responses, but idle users still need
    # bounded retention without rerunning the duplicate backfill.
    with write_session_scope(notify=False) as session:
        purge_expired_cache(session, ttl_days=get_settings().llm_cache_ttl_days)
    if run_maintenance:
        run_startup_maintenance()


def startup_maintenance_pending() -> bool:
    with session_scope() as session:
        setting = session.get(AppSetting, _MAINTENANCE_KEY)
        return setting is None or setting.value != _MAINTENANCE_VERSION


def ensure_startup_maintenance_complete() -> None:
    if startup_maintenance_pending():
        raise StartupMaintenanceRequiredError()


def run_startup_maintenance(*, stop_requested: Callable[[], bool] | None = None) -> bool:
    """Run versioned repairs once, without marking an interrupted pass complete."""
    if not startup_maintenance_pending():
        return True
    steps = (
        backfill_shortlisted_at, clear_archived_shortlists, clear_unanalyzed_shortlists,
        backfill_analyzed_statuses, backfill_usage_job_external_ids,
    )
    for step in steps:
        if stop_requested and stop_requested():
            return False
        step()
    try:
        backfill_duplicate_reviews(stop_requested=stop_requested)
    except _MaintenanceInterrupted:
        return False
    if stop_requested and stop_requested():
        return False
    from smartapply.database.display_metadata import backfill_display_metadata

    backfill_display_metadata(stop_requested=stop_requested)
    if stop_requested and stop_requested():
        return False
    with write_session_scope() as session:
        clear_shortlist_for_sent_applications(session)
        purge_expired_cache(session, ttl_days=get_settings().llm_cache_ttl_days)
        setting = session.get(AppSetting, _MAINTENANCE_KEY)
        if setting is None:
            session.add(AppSetting(key=_MAINTENANCE_KEY, value=_MAINTENANCE_VERSION))
        else:
            setting.value = _MAINTENANCE_VERSION
    return True


class _MaintenanceInterrupted(RuntimeError):
    pass


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


def _confirmed_duplicate_components(jobs: list[Job]) -> dict[int, tuple[Job, ...]]:
    """Index identity evidence without treating pending reviews as confirmed."""
    parents = {int(job.id): int(job.id) for job in jobs}

    def root(job_id: int) -> int:
        while parents[job_id] != job_id:
            parents[job_id] = parents[parents[job_id]]
            job_id = parents[job_id]
        return job_id

    for job in jobs:
        canonical_id = job.canonical_job_id
        if (
            job.duplicate_review_status == JobDuplicateStatus.CONFIRMED
            and canonical_id in parents
        ):
            parents[root(int(job.id))] = root(int(canonical_id))

    groups: dict[int, list[Job]] = {}
    for job in jobs:
        groups.setdefault(root(int(job.id)), []).append(job)
    members = {group_id: tuple(group) for group_id, group in groups.items()}
    return {id(job): members[root(int(job.id))] for job in jobs}


def backfill_duplicate_reviews(
    *, stop_requested: Callable[[], bool] | None = None, batch_size: int = 100
) -> int:
    """Repair legacy fuzzy reviews without merging or deleting rows.

    New ingests perform the same checks before persistence. This idempotent pass
    protects databases created before duplicate review existed, including rows
    that already own an archived or active application.
    """
    from smartapply.pipeline.snapshots import job_snapshot_key

    with session_scope() as session:
        jobs = session.execute(select(Job).order_by(Job.id.asc())).scalars().all()
        keys = {int(job.id): job_snapshot_key(job) for job in jobs}
        session.expunge_all()
    changed = _plan_duplicate_reviews(jobs, stop_requested=stop_requested)
    from smartapply.dedup.deduplicator import linked_duplicate_components

    components = linked_duplicate_components(jobs)
    by_id = {int(job.id): job for job in jobs}
    updated = 0
    ids = sorted(changed)
    for offset in range(0, len(ids), max(1, batch_size)):
        if stop_requested and stop_requested():
            raise _MaintenanceInterrupted()
        with write_session_scope() as session:
            for job_id in ids[offset:offset + max(1, batch_size)]:
                planned = by_id[job_id]
                current = session.get(Job, job_id)
                for member in components.get(id(planned), (planned,)):
                    reference = session.get(Job, int(member.id))
                    if reference is None or job_snapshot_key(reference) != keys[int(member.id)]:
                        raise _MaintenanceInterrupted()
                for field, value in zip(_REVIEW_FIELDS, _review_state(planned), strict=True):
                    setattr(current, field, value)
                keys[job_id] = job_snapshot_key(current)
                updated += 1
    return updated


_REVIEW_FIELDS = (
    "possible_duplicate_of_id", "duplicate_review_status", "duplicate_match_type",
    "duplicate_confidence", "status", "shortlisted_at", "shortlist_origin",
)


def _review_state(job: Job) -> tuple:
    return tuple(getattr(job, field) for field in _REVIEW_FIELDS)


def _plan_duplicate_reviews(jobs: list[Job], *, stop_requested=None) -> set[int]:
    from smartapply.dedup import (
        Deduplicator,
        normalize_company,
        offers_within_duplicate_group_time_window,
    )
    from smartapply.dedup.deduplicator import (
        MAX_FUZZY_CONFIDENCE,
        eligible_for_fuzzy_duplicate_group,
        linked_duplicate_components,
        offers_share_strong_identity,
    )

    jobs_by_id = {int(job.id): job for job in jobs}
    matcher = Deduplicator()
    updated_ids: set[int] = set()
    initial_review_states = {
        int(job.id): (
            job.possible_duplicate_of_id,
            job.duplicate_review_status,
            job.duplicate_match_type,
            job.duplicate_confidence,
            job.status,
            job.shortlisted_at,
            job.shortlist_origin,
        )
        for job in jobs
    }
    confirmed_components = _confirmed_duplicate_components(jobs)

    def release_pending_review(job: Job) -> None:
        job.possible_duplicate_of_id = None
        job.duplicate_review_status = JobDuplicateStatus.NONE
        job.duplicate_match_type = None
        job.duplicate_confidence = None
        updated_ids.add(int(job.id))

    def identity_evidence(job: Job, candidate: Job):
        # A pending sibling is a constraint, never positive evidence that
        # this offer matches the stored reference. Only that reference and
        # its confirmed aliases can support the review or its confidence.
        members = tuple(
            member
            for member in confirmed_components.get(id(candidate), (candidate,))
            if member is not job
        )
        strong_identity = any(offers_share_strong_identity(job, member) for member in members)
        match = (
            matcher.find_probable_duplicate(job, members, components=confirmed_components)
            if not strong_identity and (job.duplicate_match_type or "fuzzy") == "fuzzy"
            else None
        )
        return strong_identity, match

    # Remove unsupported edges first, before using the remaining graph for
    # conservative location/date constraints. Identity evidence is fixed
    # because this repair never changes confirmed aliases or source data.
    for job in jobs:
        if stop_requested and stop_requested():
            raise _MaintenanceInterrupted()
        if job.duplicate_review_status != JobDuplicateStatus.PENDING:
            continue
        candidate = jobs_by_id.get(int(job.possible_duplicate_of_id or 0))
        if candidate is None:
            release_pending_review(job)
            continue
        fuzzy_review = (job.duplicate_match_type or "fuzzy") == "fuzzy"
        strong_identity, recomputed_match = identity_evidence(job, candidate)
        invalid_fuzzy_identity = fuzzy_review and not strong_identity and recomputed_match is None
        invalid_exact_identity = job.duplicate_match_type == "exact_url" and not strong_identity
        if invalid_fuzzy_identity or invalid_exact_identity:
            release_pending_review(job)
            continue

        if fuzzy_review and not strong_identity:
            if job.duplicate_confidence != recomputed_match.confidence:
                job.duplicate_confidence = recomputed_match.confidence
                updated_ids.add(int(job.id))
        elif (
            fuzzy_review
            and job.duplicate_confidence is not None
            and job.duplicate_confidence > MAX_FUZZY_CONFIDENCE
        ):
            # A legacy row protected by a strong identity may not satisfy
            # the fuzzy matcher, but it still must not display 100% fuzzy
            # certainty.
            job.duplicate_confidence = MAX_FUZZY_CONFIDENCE
            updated_ids.add(int(job.id))

    linked_components = linked_duplicate_components(jobs)
    for job in jobs:
        if stop_requested and stop_requested():
            raise _MaintenanceInterrupted()
        if job.duplicate_review_status != JobDuplicateStatus.PENDING:
            continue
        candidate = jobs_by_id[int(job.possible_duplicate_of_id)]
        strong_identity, _ = identity_evidence(job, candidate)
        if not offers_within_duplicate_group_time_window(
            job, candidate, jobs, components=linked_components
        ) or (
            (job.duplicate_match_type or "fuzzy") == "fuzzy"
            and not strong_identity
            and not eligible_for_fuzzy_duplicate_group(
                job, candidate, jobs, components=linked_components
            )
        ):
            release_pending_review(job)

    # Rebuild after all removals. New edges must be compatible with both
    # complete groups, including reviews already pointing to the target.
    linked_components = linked_duplicate_components(jobs)
    candidates_by_company: dict[str, list[Job]] = {}
    for job in jobs:
        if stop_requested and stop_requested():
            raise _MaintenanceInterrupted()
        review_status = job.duplicate_review_status or JobDuplicateStatus.NONE
        company_key = normalize_company(job.company)
        previous_candidates = candidates_by_company.get(company_key, ())
        if review_status == JobDuplicateStatus.NONE:
            candidate = matcher.find_probable_duplicate(
                job,
                previous_candidates,
                components=linked_components,
            )
            target_members = linked_components.get(id(job), (job,))
            if candidate is not None and len(target_members) > 1:
                # The normal matcher checks this offer against the full
                # reference group. Incoming reviews also require every
                # target-side member to remain compatible with that group.
                compatible_candidates = [
                    previous
                    for previous in previous_candidates
                    if previous not in target_members
                    and all(
                        eligible_for_fuzzy_duplicate_group(
                            member, previous, jobs, components=linked_components
                        )
                        for member in target_members
                    )
                ]
                candidate = matcher.find_probable_duplicate(
                    job,
                    compatible_candidates,
                    components=linked_components,
                )
            if candidate is not None and int(candidate.job.id) != int(job.id):
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
                    updated_ids.add(int(job.id))
                else:
                    job.possible_duplicate_of_id = int(candidate.job.id)
                    job.duplicate_review_status = JobDuplicateStatus.PENDING
                    job.duplicate_match_type = candidate.match_type
                    job.duplicate_confidence = candidate.confidence
                    # An old shortlist marker must not remain actionable while
                    # the user is deciding. Existing analysis/application
                    # history stays intact and is still used by the guard.
                    job.shortlisted_at = None
                    job.shortlist_origin = None
                    updated_ids.add(int(job.id))
                    linked_components = linked_duplicate_components(jobs)
        if company_key:
            candidates_by_company.setdefault(company_key, []).append(job)

    # A chosen reference can have a confirmed alias outside the earlier
    # company bucket. Normalize against those fixed identity members now;
    # later pending reviews cannot inflate an earlier link's confidence.
    for job in jobs:
        if stop_requested and stop_requested():
            raise _MaintenanceInterrupted()
        if (
            job.duplicate_review_status != JobDuplicateStatus.PENDING
            or (job.duplicate_match_type or "fuzzy") != "fuzzy"
        ):
            continue
        candidate = jobs_by_id.get(int(job.possible_duplicate_of_id or 0))
        if candidate is None:
            release_pending_review(job)
            continue
        strong_identity, recomputed_match = identity_evidence(job, candidate)
        if strong_identity:
            if (
                job.duplicate_confidence is not None
                and job.duplicate_confidence > MAX_FUZZY_CONFIDENCE
            ):
                job.duplicate_confidence = MAX_FUZZY_CONFIDENCE
                updated_ids.add(int(job.id))
            continue
        if recomputed_match is not None and job.duplicate_confidence != recomputed_match.confidence:
            job.duplicate_confidence = recomputed_match.confidence
            updated_ids.add(int(job.id))
    return {
        job_id for job_id in updated_ids
        if _review_state(jobs_by_id[job_id]) != initial_review_states[job_id]
    }


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


@contextmanager
def write_session_scope(*, notify: bool = True) -> Generator[Session, None, None]:
    """Serialize short application writes while reads remain concurrent."""
    changed = False
    topics: set[str] = set()

    def observe_execute(state) -> None:
        nonlocal changed
        changed = changed or state.is_update or state.is_delete or state.is_insert
        if state.is_update or state.is_delete or state.is_insert:
            table = getattr(state.statement, "table", None)
            if table is not None:
                topics.add(table.name)
            elif state.bind_mapper is not None:
                topics.add(state.bind_mapper.persist_selectable.name)

    def observe_flush(session, _context, _instances) -> None:
        nonlocal changed
        changed = changed or bool(session.new or session.dirty or session.deleted)
        topics.update(item.__table__.name
                      for item in (*session.new, *session.dirty, *session.deleted))

    with _write_lock, session_scope() as session:
        event.listen(session, "do_orm_execute", observe_execute)
        event.listen(session, "before_flush", observe_flush)
        try:
            yield session
            changed = changed or bool(session.new or session.dirty or session.deleted)
            topics.update(item.__table__.name
                          for item in (*session.new, *session.dirty, *session.deleted))
        finally:
            event.remove(session, "do_orm_execute", observe_execute)
            event.remove(session, "before_flush", observe_flush)
    if notify and changed:
        with _observer_lock:
            callbacks = tuple(_database_observers.values())
        for callback, with_topics in callbacks:
            try:
                if with_topics:
                    callback(frozenset(topics))
                else:
                    callback()
            except Exception:
                logger.exception("Database change observer failed")


def subscribe_database_changes(
    callback: Callable[..., None], *, with_topics: bool = False
) -> Callable[[], None]:
    """Notify after committed writes, outside the writer lock, on the caller thread."""
    token = object()
    with _observer_lock:
        _database_observers[token] = callback, with_topics

    def unsubscribe() -> None:
        with _observer_lock:
            _database_observers.pop(token, None)

    return unsubscribe


def reset_engine_cache() -> None:
    """Clear cached engine/session — useful in tests that switch DB urls."""
    get_engine.cache_clear()
    get_session_factory.cache_clear()
