"""Fit requests retain explicit call routes, facts and provider identity."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.llm.fit_context import (
    build_fit_context,
    build_fit_user,
    context_is_current,
    fingerprint,
)
from smartapply.llm.fit_request import check_request_budget, complete_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.process.analysis_checkpoint import (
    extraction_key,
    load_extraction,
    save_extraction,
)
from smartapply.profile import get_profile
from tests.fit_fixtures import decision_response
from tests.llm_config_helpers import change_call, configure_calls
from tests.test_batch_analysis import extraction_offer, offer, seed_jobs


def _fit_route(provider, model, reasoning, max_output_tokens=12000):
    return {
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "max_output_tokens": max_output_tokens,
    }


def test_provider_change_preserves_fit_inputs_contract_and_policy(monkeypatch, tmp_path):
    path = configure_calls(
        monkeypatch, tmp_path, job_fit_assessment=_fit_route("openai", "gpt-5.4-mini", "medium")
    )
    profile = get_profile()
    first = build_fit_context(profile, "gpt-5.4-mini", provider="openai")
    change_call(path, "job_fit_assessment", **_fit_route("anthropic", "claude-haiku-4-5", "auto"))
    second = build_fit_context(profile, "claude-haiku-4-5", provider="anthropic")
    source = {"analyzer_input": {"offer_body": "Build Python prediction models."}}
    assert build_fit_user(first, offer(), extraction_provenance=source) == build_fit_user(
        second, offer(), extraction_provenance=source
    )
    assert "fit_annotations.technical_findings" in second["system_prompt"]
    assert first["system_prompt"] != second["system_prompt"]
    for key in ("user_template", "schema", "policy", "candidate_facts"):
        assert first[key] == second[key]
    assert first["context_fingerprint"] != second["context_fingerprint"]


def test_pending_context_keeps_its_route_when_json_changes(monkeypatch, tmp_path):
    path = configure_calls(
        monkeypatch, tmp_path, job_fit_assessment=_fit_route("openai", "gpt-5.4-mini", "medium")
    )
    profile = get_profile().model_copy(deep=True)
    context = build_fit_context(profile, "gpt-5.4-mini", provider="openai")
    frozen = copy.deepcopy(context)
    change_call(
        path, "job_fit_assessment", **_fit_route("anthropic", "claude-haiku-5-5", "high", 16000)
    )
    assert context_is_current(context, profile)
    assert context == frozen
    profile.skills.categories[0].skills.append("Changed factual skill")
    assert not context_is_current(context, profile)


def test_signed_historical_openai_context_without_provider_remains_readable(monkeypatch, tmp_path):
    configure_calls(
        monkeypatch, tmp_path, job_fit_assessment=_fit_route("openai", "gpt-5.4-mini", "medium")
    )
    profile = get_profile()
    context = build_fit_context(profile, "gpt-5.4-mini", provider="openai")
    context.pop("provider")
    context["context_fingerprint"] = fingerprint(
        {key: value for key, value in context.items() if key != "context_fingerprint"}
    )
    assert context_is_current(context, profile)


@pytest.mark.parametrize(
    ("model", "window"),
    [
        ("claude-haiku-4-5", 200000),
        ("claude-haiku-4-5-20251001", 200000),
        ("claude-sonnet-4-6", 1000000),
    ],
)
def test_claude_fit_budget_uses_the_model_window(monkeypatch, tmp_path, model, window):
    reasoning = "high" if model.startswith("claude-sonnet-4-6") else "auto"
    configure_calls(
        monkeypatch, tmp_path, job_fit_assessment=_fit_route("anthropic", model, reasoning)
    )
    context = build_fit_context(get_profile(), model, provider="anthropic")
    context["configured_context_window"] = None
    user = build_fit_user(context, offer())
    budget = check_request_budget(context, user)
    assert budget["context_window"] == window
    assert budget["output_reserved"] == 12000
    context["configured_context_window"] = (
        budget["input_token_upper_bound"] + budget["output_reserved"] - 1
    )
    with pytest.raises(LLMValidationError, match="no factual content was truncated"):
        check_request_budget(context, user)


def test_unknown_claude_model_requires_explicit_context_window(monkeypatch, tmp_path):
    configure_calls(
        monkeypatch, tmp_path, job_fit_assessment=_fit_route("anthropic", "claude-custom", "auto")
    )
    context = build_fit_context(get_profile(), "claude-custom", provider="anthropic")
    context["configured_context_window"] = None
    with pytest.raises(LLMValidationError, match="Unknown model context window"):
        check_request_budget(context, "Source unchanged")
    context["configured_context_window"] = 300000
    assert check_request_budget(context, "Source unchanged")["context_window"] == 300000


@pytest.mark.parametrize(
    ("provider_name", "model", "other"),
    [("openai", "gpt-5.4-mini", "anthropic"), ("anthropic", "claude-haiku-4-5", "openai")],
)
def test_frozen_fit_refuses_another_provider_before_call(
    monkeypatch, tmp_path, provider_name, model, other
):
    configure_calls(
        monkeypatch,
        tmp_path,
        job_fit_assessment=_fit_route(
            provider_name, model, "medium" if provider_name == "openai" else "auto"
        ),
    )
    context = build_fit_context(get_profile(), model, provider=provider_name)
    calls = []
    wrong = SimpleNamespace(name=other, complete_json=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(LLMValidationError, match="Fit provider does not match"):
        complete_fit(wrong, context, offer())
    assert not calls


def test_fit_keeps_scoring_and_observations_without_extra_call(monkeypatch, tmp_path):
    configure_calls(
        monkeypatch,
        tmp_path,
        job_fit_assessment=_fit_route("anthropic", "claude-haiku-4-5", "auto"),
    )
    context = build_fit_context(get_profile(), "claude-haiku-4-5", provider="anthropic")
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return {**decision_response(), "issues": "invalid annotations"}

    score, diagnostics = complete_fit(
        SimpleNamespace(name="anthropic", complete_json=complete), context, offer()
    )
    assert len(calls) == 1 and calls[0]["model"] == "claude-haiku-4-5"
    assert score.fit_score == 0.95
    assert diagnostics["diagnostic_checks"]["warnings"]


def test_extraction_checkpoints_remain_isolated_by_provider(isolated_db):
    job_id = seed_jobs()[0]
    with session_scope() as session:
        job = session.get(Job, job_id)
        source = build_analyzer_input(job)
        openai_key = extraction_key(source, "shared-model-alias", provider="openai")
        claude_key = extraction_key(source, "shared-model-alias", provider="anthropic")
        assert claude_key != openai_key
        save_extraction(session, job, openai_key, extraction_offer())
        assert load_extraction(session, job, openai_key) is not None
        assert load_extraction(session, job, claude_key) is None
