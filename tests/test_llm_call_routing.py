"""Independent JSON routes keep exact provider, model and request identity."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.models import LLMCache
from smartapply.desktop.llm_settings import reasoning_options, save_llm_calls
from smartapply.llm.factory import get_llm_provider
from smartapply.llm.openai_provider import OpenAIProvider
from smartapply.llm.routes import provider_for
from smartapply.llm.structured import Completion
from smartapply.llm_policy import CALL_TYPES, CallConfigurationError, resolve_route
from smartapply.pipeline.errors import TaskCancelledError
from smartapply.pipeline.snapshots import CancellableProvider
from tests.llm_config_helpers import change_call, configure_calls


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize("call", CALL_TYPES, ids=lambda call: call.purpose)
def test_each_call_can_change_without_rerouting_another(call, monkeypatch, tmp_path):
    path = configure_calls(monkeypatch, tmp_path)
    before = get_llm_settings()
    change_call(
        path, call.purpose, provider="anthropic", model="claude-opus-4-6",
        reasoning="max", max_output_tokens=8192,
    )
    after = get_llm_settings()
    route = resolve_route(after, call.purpose)
    assert (route.provider, route.model, route.reasoning, route.max_output_tokens) == (
        "anthropic", "claude-opus-4-6", "max", 8192,
    )
    for other in CALL_TYPES:
        if other != call:
            assert resolve_route(after, other.purpose) == resolve_route(before, other.purpose)


@pytest.mark.parametrize(
    ("provider", "model", "levels"),
    [
        ("openai", "gpt-5.4-mini", ["auto", "none", "low", "medium", "high", "xhigh"]),
        ("openai", "gpt-5.4-pro", ["auto", "medium", "high", "xhigh"]),
        ("anthropic", "claude-opus-4-6", ["auto", "low", "medium", "high", "max"]),
        ("openai", "gpt-custom", ["auto"]),
    ],
)
def test_reasoning_choices_follow_selected_model(provider, model, levels):
    assert reasoning_options(provider, model) == levels


@pytest.mark.parametrize(
    ("provider", "model", "reasoning", "field"),
    [
        ("anthropic", "claude-opus-4-5", "max", "reasoning"),
        ("anthropic", "claude-opus-4-6", "xhigh", "reasoning"),
        ("openai", "gpt-5.4-pro", "none", "reasoning"),
        ("openai", "invalid-model", "auto", "model"),
    ],
)
def test_invalid_combination_keeps_saved_json(
    monkeypatch, tmp_path, provider, model, reasoning, field
):
    path = configure_calls(monkeypatch, tmp_path)
    previous = path.read_bytes()
    with pytest.raises(CallConfigurationError, match=f"job_fit_assessment.{field}"):
        save_llm_calls([{
            "purpose": "job_fit_assessment", "provider": provider, "model": model,
            "reasoning": reasoning, "max_output_tokens": 12000,
        }])
    assert path.read_bytes() == previous


def test_route_snapshot_and_cache_identity_follow_json_changes(isolated_db, monkeypatch, tmp_path):
    path = configure_calls(
        monkeypatch, tmp_path,
        application_texts={
            "provider": "openai", "model": "gpt-5.4", "reasoning": "low",
            "max_output_tokens": 6000,
        },
    )
    calls = []

    def generate(provider, body):
        calls.append((body["model"], body["reasoning_effort"]))
        return Completion(content='{"answer":"ok"}', model=body["model"])

    monkeypatch.setattr(OpenAIProvider, "_generate", generate)
    old_router = get_llm_provider()
    old_provider = provider_for(old_router, "application_texts")
    assert old_provider.complete_json(
        system="same", user="same", schema=Answer, purpose="application_texts"
    ).answer == "ok"
    change_call(path, "application_texts", reasoning="high")
    new_provider = provider_for(get_llm_provider(), "application_texts")
    assert new_provider.complete_json(
        system="same", user="same", schema=Answer, purpose="application_texts"
    ).answer == "ok"
    assert old_provider.complete_json(
        system="same", user="same", schema=Answer, purpose="application_texts"
    ).answer == "ok"
    assert calls == [("gpt-5.4", "low"), ("gpt-5.4", "high")]
    with session_scope() as session:
        assert len(list(session.scalars(select(LLMCache)))) == 2


def test_cancellation_precedes_a_provider_request(monkeypatch, tmp_path):
    path = configure_calls(monkeypatch, tmp_path)
    change_call(path, "document_review", provider="")
    with pytest.raises(CallConfigurationError, match="document_review.provider"):
        provider_for(CancellableProvider(get_llm_provider(), lambda: True), "document_review")
    change_call(path, "document_review", provider="mock", reasoning="auto")
    selected = provider_for(CancellableProvider(get_llm_provider(), lambda: True), "document_review")
    with pytest.raises(TaskCancelledError):
        selected.complete_json()


def test_router_does_not_replace_an_explicitly_requested_model(monkeypatch, tmp_path):
    configure_calls(monkeypatch, tmp_path, application_texts={
        "provider": "openai", "model": "gpt-5.4", "reasoning": "medium",
        "max_output_tokens": 6000,
    })
    router = get_llm_provider()
    with pytest.raises(CallConfigurationError, match="application_texts.model"):
        router.complete_json(
            system="s", user="u", schema=Answer, purpose="application_texts",
            model="gpt-5.4-mini",
        )
