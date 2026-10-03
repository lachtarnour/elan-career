"""Offer reservations and exclusive publication of application documents."""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, get_ident

from sqlalchemy import or_, select

_lock = RLock()
_claims: dict[int, tuple[int, str, int]] = {}
_subscribers: set[Callable[[], None]] = set()


class TaskConflictError(RuntimeError):
    """A contradictory operation already owns this offer or one of its aliases."""

    def __init__(self, job_id: int, operation: str):
        self.job_id = job_id
        super().__init__(
            f"L’offre {job_id} est utilisée par « {operation} ». "
            "Attendez sa fin ou annulez cette tâche avant de modifier son état."
        )


def busy_job_ids() -> list[int]:
    with _lock:
        return sorted(_claims)


def subscribe_resource_changes(callback: Callable[[], None]) -> Callable[[], None]:
    """Subscribe to reservations; callbacks may run on any worker thread."""
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
        for job_id in members:
            claim = _claims.get(job_id)
            if claim is not None and claim[0] != owner:
                raise TaskConflictError(job_id, claim[1])
        for job_id in members:
            claim = _claims.get(job_id)
            _claims[job_id] = (
                owner,
                operation if claim is None else claim[1],
                1 if claim is None else claim[2] + 1,
            )
    _notify()
    try:
        yield
    finally:
        with _lock:
            for job_id in members:
                claim = _claims[job_id]
                if claim[2] == 1:
                    del _claims[job_id]
                else:
                    _claims[job_id] = (claim[0], claim[1], claim[2] - 1)
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
