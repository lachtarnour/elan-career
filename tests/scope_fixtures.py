"""Completed neutral prefilters for tests focused on extraction or later steps."""

import copy
import json

from smartapply.config import get_llm_settings
from smartapply.llm.schemas import JobScopeScreening
from smartapply.llm_policy import resolve_route
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.process.scope_screening import (
    save_scope,
    scope_context,
    scope_request,
    scope_result,
)


def configured_scope_context():
    settings = get_llm_settings()
    route = resolve_route(settings, "job_scope_screening")
    return scope_context(
        provider=route.provider,
        model=route.model,
        settings=settings,
        max_completion_tokens=route.max_output_tokens,
    )


def seed_neutral_scope(session, job):
    settings = get_llm_settings()
    configured = resolve_route(settings, "job_scope_screening")
    routes = {
        ("openai", "mock", None),
        ("openai", "mock-cheap", None),
        ("mock", "mock", None),
        ("mock", "mock-cheap", None),
        ("mock", "mock-job_scope_screening", None),
        ("openai", "mock-job_scope_screening", None),
        ("openai", "gpt-5.4-mini", None),
        ("anthropic", "claude-haiku-5-5", None),
        ("anthropic", "claude-haiku-5-5", "high"),
    }
    routes.add((configured.provider, configured.model, configured.reasoning))
    verdict = JobScopeScreening(
        mission_screen={
            "mission_relation": "unknown",
            "mission_evidence": "insufficient",
        }
    )
    for provider, model, effort in routes:
        context = scope_context(
            provider=provider,
            model=model,
            settings=settings,
            max_completion_tokens=configured.max_output_tokens,
            effort=effort if effort != "auto" else None,
        )
        request = scope_request(context, build_analyzer_input(job))
        save_scope(session, job.id, request.cache_key, scope_result(verdict, context=context))


def set_test_routes(monkeypatch, tmp_path, calls):
    """Use an isolated explicit JSON snapshot without changing runtime configuration."""
    payload = copy.deepcopy(get_llm_settings().calls)
    payload.update(calls)
    path = tmp_path / "test-model-config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("smartapply.model_config.model_config_file", lambda: path)
    return path
