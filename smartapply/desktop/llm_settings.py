"""Validated settings for the independent LLM calls and the application."""

from __future__ import annotations

import fcntl
import os
import tempfile
from copy import deepcopy
from io import StringIO
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from dotenv.parser import parse_stream

from smartapply import config
from smartapply.llm.request_options import supported_efforts
from smartapply.llm.routes import CALL_TYPES, resolve_route
from smartapply.llm_policy import INHERITED_CALLS, CallConfigurationError
from smartapply.model_config import model_config_file, read_model_config, write_model_config

_WRITE_LOCK = Lock()
_PROVIDERS = ("openai", "anthropic")
_APP_GROUPS = (
    ("Données", ("database_url", "profile_dir", "output_dir", "cache_dir")),
    ("Cache et pipeline", (
        "llm_cache_ttl_days", "llm_max_concurrent", "top_k_ranked",
        "dedup_title_threshold", "dedup_desc_threshold", "declared_experience",
    )),
    ("Documents", ("cv_merge_sparse_secondary_skills", "cv_secondary_skill_block_min_size")),
    ("Fenêtres de contexte", ("openai_context_window_tokens", "anthropic_context_window_tokens")),
    ("Sources", tuple(
        name for name in config.Settings.model_fields
        if name.startswith(("serpapi_", "francetravail_", "apify_", "linkedin_", "wttj_"))
    )),
    ("Journalisation", ("log_level",)),
)
_APP_FIELDS = {name for _, names in _APP_GROUPS for name in names}


def _is_secret(name: str) -> bool:
    key = name.lower()
    return key == "database_url" or key.endswith(
        ("_api_key", "_token", "_secret", "_cookie", "_password")
    )


def application_configuration() -> list[dict]:
    settings = config.Settings(_env_file=str(config.ENV_FILE))
    fields = []
    for group, names in _APP_GROUPS:
        for name in names:
            value = getattr(settings, name)
            secret = _is_secret(name)
            fields.append({
                "key": name.upper(), "label": name.replace("_", " ").capitalize(),
                "group": group, "value": "" if secret or value is None else str(value),
                "secret": secret, "configured": bool(value) if secret else False,
            })
    return fields


def save_application_settings(updates: dict[str, str]) -> dict:
    if not isinstance(updates, dict) or not updates:
        raise ValueError("Aucun paramètre d’application à enregistrer.")
    changed = {}
    for key, value in updates.items():
        if key.lower() not in _APP_FIELDS:
            raise ValueError(f"Paramètre d’application inconnu : {key}.")
        clean = _single_line(value, label=key, required=False)
        if _is_secret(key) and not clean:
            continue
        changed[key.upper()] = (
            None if key.lower() in {"openai_context_window_tokens", "anthropic_context_window_tokens"}
            and not clean else clean
        )
    if not changed:
        raise ValueError("Aucune nouvelle valeur à enregistrer.")

    def validate():
        from pydantic import ValidationError

        current = config.Settings(_env_file=str(config.ENV_FILE))
        try:
            config.Settings(
                _env_file=None,
                **(current.model_dump() | {name.lower(): value for name, value in changed.items()}),
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            field = str(first.get("loc", ("paramètre",))[0]).upper()
            raise ValueError(f"Valeur invalide pour {field}. Vérifiez ce paramètre.") from None

    _atomic_update(config.ENV_FILE, changed, validate=validate)
    return llm_configuration()


def llm_configuration() -> dict:
    """Expose names and credential availability, never credential values."""
    settings = config.get_llm_settings()
    calls = []
    for call in CALL_TYPES:
        raw = settings.calls.get(call.purpose, {})
        raw = raw if isinstance(raw, dict) else {}
        provider = raw.get("provider") or ""
        model = raw.get("model") or ""
        reasoning = raw.get("reasoning") or ""
        max_output_tokens = raw.get("max_output_tokens")
        error = ""
        try:
            route = resolve_route(settings, call.purpose)
            provider, model, reasoning = route.provider, route.model, route.reasoning or "auto"
            max_output_tokens = route.max_output_tokens
        except CallConfigurationError as exc:
            error = str(exc)
        calls.append({
            "purpose": call.purpose,
            "label": call.label,
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "max_output_tokens": max_output_tokens or "",
            "reasoning_options": reasoning_options(provider, model),
            "error": error,
        })
    return {
        "calls": calls,
        "api_keys": {name: bool(getattr(settings, f"{name}_api_key")) for name in _PROVIDERS},
        "app_fields": application_configuration(),
        "model_config_path": str(model_config_file()),
        "env_path": str(config.ENV_FILE),
    }


def reasoning_options(provider: str, model: str) -> list[str]:
    allowed = supported_efforts(provider, model) or frozenset()
    return [
        "auto",
        *[
            value
            for value in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
            if value in allowed
        ],
    ]


def _single_line(value: str, *, label: str, required: bool = True) -> str:
    normalized = str(value).strip()
    if "\n" in normalized or "\r" in normalized or "\x00" in normalized:
        raise ValueError(f"{label} doit tenir sur une seule ligne.")
    if required and not normalized:
        raise ValueError(f"{label} est requis.")
    return normalized


def _render_updates(original: str, updates: dict[str, str | None]) -> str:
    remaining = dict(updates)
    rows = []
    for binding in parse_stream(StringIO(original)):
        row = binding.original.string
        key = binding.key.upper() if binding.key else ""
        if key not in updates:
            rows.append(row)
        elif key in remaining:
            value = remaining.pop(key)
            if value is not None:
                value = value.replace("\\", "\\\\").replace("'", "\\'")
                rows.append(f"{key}='{value}'\n")
        # Remove duplicate assignments only for the settings being changed.
    if rows and not rows[-1].endswith(("\n", "\r")):
        rows[-1] += "\n"
    for key, value in remaining.items():
        if value is None:
            continue
        value = value.replace("\\", "\\\\").replace("'", "\\'")
        rows.append(f"{key}='{value}'\n")
    return "".join(rows)


def _atomic_update(path: Path, updates: dict[str, str | None], *, validate=None) -> None:
    if not path.is_file():
        raise ValueError(f"Fichier de configuration absent : {path}.")
    lock_path = path.with_name(f".{path.name}.llm.lock")
    with _WRITE_LOCK, lock_path.open("a", encoding="utf-8") as lock_file:
        lock_path.chmod(0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        temporary = None
        try:
            if validate is not None:
                validate()
            original = path.read_text(encoding="utf-8") if path.exists() else ""
            updated = _render_updates(original, updates)
            descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(updated)
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


def save_llm_api_key(provider: str, api_key: str) -> dict:
    """Persist only a provider credential, without changing any call route."""
    provider = _single_line(provider, label="Le fournisseur").lower()
    if provider == "claude":
        provider = "anthropic"
    if provider not in _PROVIDERS:
        raise ValueError("Choisissez OpenAI ou Claude.")
    api_key = _single_line(api_key, label="La clé API")
    _atomic_update(config.ENV_FILE, {f"{provider.upper()}_API_KEY": api_key})
    return llm_configuration()


def save_llm_calls(calls: list[dict]) -> dict:
    """Validate all call choices before one atomic save; never substitute effort."""
    updates = {}
    known = {call.purpose for call in CALL_TYPES}
    seen = set()
    for call in calls:
        purpose = call.get("purpose")
        if purpose not in known or purpose in seen:
            raise ValueError("Type d’appel IA inconnu ou présent plusieurs fois.")
        seen.add(purpose)
        for field in ("provider", "model", "reasoning"):
            value = _single_line(call.get(field, ""), label=f"{purpose} · {field}", required=False)
            updates.setdefault(purpose, {})[field] = value
        token_value = call.get("max_output_tokens")
        if isinstance(token_value, str) and token_value.strip().isdigit():
            token_value = int(token_value.strip())
        updates[purpose]["max_output_tokens"] = token_value
    if not updates:
        raise ValueError("Aucun réglage IA à enregistrer.")
    original = read_model_config()
    candidate = {
        purpose: deepcopy(route)
        for purpose, route in original.items() if purpose not in INHERITED_CALLS
    }
    candidate.update(updates)
    snapshot = SimpleNamespace(calls=candidate, load_error="")
    for purpose in updates:
        resolve_route(snapshot, purpose)
    write_model_config(candidate, expected=original)
    return llm_configuration()
