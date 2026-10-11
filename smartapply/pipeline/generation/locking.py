"""Nonblocking, reentrant execution locks scoped to the database and optional run."""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, local

from smartapply.config import get_settings
from smartapply.database import get_engine

_registry_lock = Lock()
_locks: dict[str, RLock] = {}
_owned = local()


@contextmanager
def generation_operation(run_id: str | None = None):
    url = get_engine().url
    database = url.database
    identity = url.render_as_string(hide_password=False)
    key = hashlib.sha256(f"{identity}:{run_id or 'transport'}".encode()).hexdigest()[:24]
    with _registry_lock:
        lock = _locks.setdefault(key, RLock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("Le suivi de cette création est déjà en cours.")
    depths = getattr(_owned, "depths", {})
    _owned.depths = depths
    nested = key in depths
    try:
        if nested:
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        directory = (
            Path(database).expanduser().resolve().parent / ".elan-locks"
            if url.get_backend_name() == "sqlite" and database not in {None, "", ":memory:"}
            else get_settings().cache_dir / ".elan-locks"
        )
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"generation-{key}.lock").open("a+b") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Une autre instance suit déjà cette création.") from exc
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        lock.release()
