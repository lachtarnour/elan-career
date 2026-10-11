"""Isolated explicit call routes for transport tests; never contact providers."""

from __future__ import annotations

import json
from pathlib import Path

from smartapply.llm_policy import CALL_TYPES, configuration_purpose


def isolated_catalogue() -> dict[str, dict]:
    """Valid isolated routes; tests never depend on the user's editable JSON."""
    analysis = {"job_scope_screening", "job_offer_extraction", "job_fit_assessment"}
    routes = {}
    for call in CALL_TYPES:
        purpose = call.purpose
        routes[purpose] = {
            "provider": "anthropic" if purpose in analysis else "openai",
            "model": "claude-haiku-5-5" if purpose in analysis else "gpt-5.4",
            "reasoning": (
                "high" if purpose in analysis else "medium" if purpose == "document_correction"
                else "low" if purpose == "document_review" else "none"
            ),
            "max_output_tokens": 32000,
        }
    return routes


def configure_calls(monkeypatch, tmp_path: Path, **routes: dict) -> Path:
    """Write a complete independent catalogue and redirect only this test to it."""
    catalogue = isolated_catalogue()
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
