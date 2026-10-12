"""Offer reservations and exclusive publication of application documents."""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, get_ident, local

from sqlalchemy import or_, select

_lock = RLock()
_claims: dict[int, tuple[int, str, int]] = {}
_subscribers: set[Callable[[], None]] = set()
_generation_context = local()


@contextmanager
def generation_owner(run_id: str | Iterable[str]) -> Iterator[None]:
    """Allow only the coordinator of a durable run to enter its reserved offers."""
    previous = getattr(_generation_context, "run_ids", frozenset())
    _generation_context.run_ids = frozenset([run_id] if isinstance(run_id, str) else run_id)
    try:
        yield
    finally:
        _generation_context.run_ids = previous


def _generation_claims(session, job_ids=None):
    from smartapply.database.generation_runs import (
        RESERVED_GENERATION_STATES,
        TERMINAL_GENERATION_ITEM_STATES,
        GenerationItem,
        GenerationRun,
    )

    query = select(GenerationItem.job_id, GenerationRun.id).join(GenerationRun).where(
        GenerationRun.state.in_(RESERVED_GENERATION_STATES),
        GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
    )
    if job_ids is not None:
        query = query.where(GenerationItem.job_id.in_(job_ids))
    return session.execute(query).all()


class TaskConflictError(RuntimeError):
    """A contradictory operation already owns this offer or one of its aliases."""

    def __init__(self, job_id: int, operation: str):
        self.job_id = job_id
        super().__init__(
            f"L’offre {job_id} est utilisée par « {operation} ». "
            "Attendez sa fin ou annulez cette tâche avant de modifier son état."
        )


def running_job_ids() -> list[int]:
    """Live local operations, without stopped durable generation reservations."""
    with _lock:
        claimed = set(_claims)
    return sorted(_group_ids(claimed)) if claimed else []


def busy_job_ids() -> list[int]:
    from smartapply.database import session_scope

    # Merge local and durable reservations before expanding aliases. Expanding
    # the local set first would repeat the same group reads on every refresh.
    with _lock:
        claimed = set(_claims)
    with session_scope() as session:
        claimed.update(job_id for job_id, _ in _generation_claims(session))
    return sorted(_group_ids(claimed)) if claimed else []


def subscribe_resource_changes(callback: Callable[[], None]) -> Callable[[], None]:
    """Observe local reservation membership changes on any worker thread."""
    with _lock:
        _subscribers.add(callback)

    def unsubscribe() -> None:
        with _lock:
            _subscribers.discard(callback)

    return unsubscribe


def _notify() -> None:
    with _lock:
        callbacks = tuple(_subscribers)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            # Observers must never interrupt a reservation's release.
            from smartapply.logging_setup import get_logger

            get_logger(__name__).exception("Resource observer failed")


def _group_ids(job_ids: Iterable[int]) -> set[int]:
    # Late imports allow database initialization to use this module too.
    from smartapply.database import session_scope
    from smartapply.database.models import Job

    members = {int(job_id) for job_id in job_ids}
    frontier = set(members)
    with session_scope() as session:
        while frontier:
            rows = session.execute(
                select(Job.id, Job.canonical_job_id, Job.possible_duplicate_of_id).where(
                    or_(
                        Job.id.in_(frontier),
                        Job.canonical_job_id.in_(frontier),
                        Job.possible_duplicate_of_id.in_(frontier),
                    )
                )
            ).all()
            discovered = {int(value) for row in rows for value in row if value is not None}
            frontier = discovered - members
            members.update(discovered)
    return members


@contextmanager
def job_operation(job_ids: Iterable[int], *, operation: str) -> Iterator[None]:
    """Reserve a duplicate group without blocking unrelated offers.

    Nested calls from the same thread are permitted. The lock only protects
    reservation bookkeeping and is released before any provider call.
    """
    owner = get_ident()
    with _lock:
        members = _group_ids(job_ids)
        from smartapply.database import session_scope

        with session_scope() as session:
            for job_id, run_id in _generation_claims(session, members):
                if run_id not in getattr(_generation_context, "run_ids", frozenset()):
                    raise TaskConflictError(job_id, "création de candidature · reprise conservée")
        for job_id in members:
            claim = _claims.get(job_id)
            if claim is not None and claim[0] != owner:
                raise TaskConflictError(job_id, claim[1])
        membership_changed = any(job_id not in _claims for job_id in members)
        for job_id in members:
            claim = _claims.get(job_id)
            _claims[job_id] = (
                owner,
                operation if claim is None else claim[1],
                1 if claim is None else claim[2] + 1,
            )
    if membership_changed:
        _notify()
    try:
        yield
    finally:
        membership_changed = False
        with _lock:
            for job_id in members:
                claim = _claims[job_id]
                if claim[2] == 1:
                    del _claims[job_id]
                    membership_changed = True
                else:
                    _claims[job_id] = (claim[0], claim[1], claim[2] - 1)
        if membership_changed:
            _notify()


def _publication_lock_path(application_id: int) -> Path:
    from smartapply.config import get_settings
    from smartapply.database import get_engine

    url = get_engine().url
    if url.get_backend_name() == "sqlite" and url.database not in {None, "", ":memory:"}:
        database = Path(url.database).expanduser().resolve()
        identity = str(database)
        directory = database.parent / ".elan-locks"
    else:
        identity = url.render_as_string(hide_password=False)
        directory = get_settings().cache_dir / ".elan-locks"
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return directory / fingerprint / f"application-{int(application_id)}.lock"


@contextmanager
def application_publication(job_id: int, application_id: int) -> Iterator[None]:
    """Prevent another local process from regenerating the same dossier.

    SQLite leases are anchored beside the canonical DB, independent of cache
    configuration. The inode stays in place after release; unlinking it would
    allow a third process to acquire a different inode while a waiter owns it.
    """
    path = _publication_lock_path(application_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskConflictError(job_id, "génération dans un autre processus") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
