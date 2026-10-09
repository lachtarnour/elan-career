"""Isolated explicit call routes for transport tests; never contact providers."""

from __future__ import annotations

import json
from pathlib import Path

from smartapply.llm_policy import configuration_purpose
from smartapply.model_config import read_model_config


def configure_calls(monkeypatch, tmp_path: Path, **routes: dict) -> Path:
    """Write a complete independent catalogue and redirect only this test to it."""
    catalogue = read_model_config()
    for purpose, route in routes.items():
        purpose = configuration_purpose(purpose)
        catalogue[purpose] = {**catalogue[purpose], **route}
    path = tmp_path / "model_config.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")
    monkeypatch.setattr("smartapply.model_config.model_config_file", lambda: path)
    return path


def change_call(path: Path, purpose: str, **updates) -> None:
    catalogue = json.loads(path.read_text(encoding="utf-8"))
    purpose = configuration_purpose(purpose)
    catalogue[purpose].update(updates)
    path.write_text(json.dumps(catalogue), encoding="utf-8")
