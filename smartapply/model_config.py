"""The single durable source of LLM call routes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from threading import Lock

_WRITE_LOCK = Lock()


def model_config_file() -> Path:
    from smartapply.config import ENV_FILE

    return ENV_FILE.with_name("model_config.json")


def read_model_config() -> dict:
    path = model_config_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration IA absente : {path}. Renseignez model_config.json.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Impossible de lire la configuration IA {path} : {exc}.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Configuration IA invalide : {path} doit contenir un objet JSON.")
    from smartapply.llm_policy import CALL_BY_PURPOSE

    unexpected = sorted(set(payload) - set(CALL_BY_PURPOSE))
    if unexpected:
        raise ValueError(
            f"Configuration IA invalide : appels inconnus dans {path} : {', '.join(unexpected)}."
        )
    return payload


def write_model_config(payload: dict, *, expected: dict) -> None:
    """Replace atomically, refusing to overwrite a concurrent edit."""
    path = model_config_file()
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with _WRITE_LOCK, lock_path.open("a", encoding="utf-8") as lock_file:
        os.fchmod(lock_file.fileno(), 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        temporary = None
        try:
            if read_model_config() != expected:
                raise ValueError("model_config.json a été modifié entre-temps. Actualisez les réglages.")
            descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            fcntl.flock(lock_file, fcntl.LOCK_UN)
