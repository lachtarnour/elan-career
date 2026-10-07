"""Reserve journal transitions across desktop workers and CLI processes."""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from sqlalchemy.engine import make_url

from smartapply.config import get_settings

_thread_lock = Lock()


@contextmanager
def batch_operation():
    settings = get_settings()
    if not _thread_lock.acquire(blocking=False):
        raise RuntimeError("Le suivi des analyses est déjà en cours. Réessayez dans un instant.")
    try:
        # Key the lock to the database, rather than a run or process instance.
        url = make_url(settings.database_url)
        database = url.database or settings.database_url
        digest = hashlib.sha256(database.encode()).hexdigest()[:20]
        path = (
            Path(database).expanduser().resolve().parent
            / f".{Path(database).name}.analysis-batch.lock"
            if url.get_backend_name() == "sqlite" and database != ":memory:"
            else settings.cache_dir / f"analysis-batch-{digest}.lock"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Une autre instance suit déjà les analyses.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        _thread_lock.release()
