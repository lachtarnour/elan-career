"""Claude transport exercises the real shared reader/cache without network calls."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import LLMCache, LLMUsage
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.anthropic_transport import build_message_params, parse_message
from smartapply.llm.cache import make_cache_key
from smartapply.llm.factory import get_llm_provider
from smartapply.llm.provider import LLMError, LLMValidationError
from smartapply.llm.schemas import JobFitDecisionEvaluation
from smartapply.llm.structured import request_body
from smartapply.llm.usage import estimate_cost_usd
from tests.fit_fixtures import decision_response
from tests.llm_config_helpers import change_call, configure_calls


class Answer(BaseModel):
    answer: str


class Bounded(BaseModel):
    level: int = Field(ge=0, le=4)


@pytest.fixture(autouse=True)
def _anthropic_test_routes(monkeypatch, tmp_path):
    from smartapply.llm_policy import CALL_TYPES

    path = configure_calls(
        monkeypatch,
        tmp_path,
        **{
            call.purpose: {
                "provider": "anthropic",
                "model": "claude-haiku-4-5",
                "reasoning": "auto",
                "max_output_tokens": 6000,
            }
            for call in CALL_TYPES
        },
    )
    return path


def message(raw='{"answer":"Claude"}', *, stop="end_turn", model="claude-haiku-4-5-20251001"):
    return {
        "model": model,
        "content": [{"type": "text", "text": raw}],
        "stop_reason": stop,
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_input_tokens": 23,
            "cache_creation_input_tokens": 31,
        },
    }


def fake_provider(monkeypatch, *, raw='{"answer":"Claude"}', stop="end_turn"):
    provider = AnthropicProvider(model="claude-haiku-4-5")
    calls = []

    def fake(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return message(raw, stop=stop)

    monkeypatch.setattr(provider, "_call_anthropic", fake)
    return provider, calls


def test_wire_translation_preserves_prompt_bytes_and_canonical_schema():
    body = request_body(
        system="Même système é\n",
        user="Même profil + offre\n",
        schema=Bounded,
        model="claude-haiku-4-5",
        temperature=0.2,
        max_completion_tokens=12000,
        purpose="fit",
    )
    frozen = copy.deepcopy(body)
    params = build_message_params(body)
    assert params["system"][0]["text"] == body["messages"][0]["content"]
    assert params["messages"][0]["content"] == body["messages"][1]["content"]
    assert params["max_tokens"] == 12000
    assert params["temperature"] == 0.2
    assert params["output_config"]["format"]["schema"]["properties"]["level"]["enum"] == list(
        range(5)
    )
    assert body == frozen
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "output_format" not in params and "tools" not in params


def test_fit_wire_schema_keeps_fixed_values_and_constants():
    body = request_body(
        system="s",
        user="u",
        schema=JobFitDecisionEvaluation,
        model="claude-haiku-4-5",
        temperature=0.2,
        max_completion_tokens=12000,
        purpose="fit",
    )
    wire = build_message_params(body)["output_config"]["format"]["schema"]
    for name, definition in body["response_format"]["json_schema"]["schema"]["$defs"].items():
        if "enum" in definition:
            assert wire["$defs"][name]["enum"] == definition["enum"]
    interpretation = wire["$defs"]["FitOfferInterpretation"]["properties"]
    assert interpretation["source_ref"]["const"] == "offer_source:body"
    assert interpretation["origin"]["const"] == "offer_information"


@pytest.mark.parametrize(
    "model,temperature",
    [
        ("claude-sonnet-4-6", 0.2),
        ("claude-opus-4-6-20260205", 0.2),
        ("claude-opus-4-7", None),
        ("claude-sonnet-5-5", None),
        ("custom-future-model", None),
    ],
)
def test_wire_temperature_is_only_sent_to_compatible_models(model, temperature):
    body = request_body(
        system="same system",
        user="same user",
        schema=Answer,
        model=model,
        temperature=0.2,
        max_completion_tokens=6000,
        purpose="answer",
    )
    params = build_message_params(body)
    assert params.get("temperature") == temperature
    assert params["system"][0]["text"] == "same system"
    assert params["messages"][0]["content"] == "same user"


def test_normalized_usage_counts_all_input_exactly_once():
    completion = parse_message(message())
    assert completion.prompt_tokens == 65
    assert completion.cached_prompt_tokens == 23
    assert completion.cache_write_prompt_tokens == 31
    assert completion.completion_tokens == 7
    assert completion.finish_reason == "stop"


def test_thinking_blocks_are_not_json_and_invalid_token_counts_fail():
    raw = message()
    raw["content"].insert(0, {"type": "thinking", "thinking": "internal"})
    assert parse_message(raw).content == '{"answer":"Claude"}'
    raw["usage"]["input_tokens"] = -1
    with pytest.raises(LLMError, match="token count"):
        parse_message(raw)


def test_immediate_cache_and_usage_are_attributed_to_claude(isolated_db, monkeypatch):
    provider, calls = fake_provider(monkeypatch)
    first = provider.complete_json(system="s", user="u", schema=Answer, purpose="document_review")
    second = provider.complete_json(system="s", user="u", schema=Answer, purpose="document_review")
    assert first == second == Answer(answer="Claude")
    assert len(calls) == 1
    with session_scope() as session:
        entries = list(session.scalars(select(LLMCache)))
        usage = list(session.scalars(select(LLMUsage).order_by(LLMUsage.id)))
        assert len(entries) == 1 and entries[0].provider == "anthropic"
        assert [row.provider for row in usage] == ["anthropic", "anthropic"]
        assert usage[0].prompt_tokens == 65
        assert usage[0].cached_prompt_tokens == 23
        assert usage[0].cache_write_prompt_tokens == 31
        assert usage[0].cost_usd == pytest.approx((11 + 23 * 0.1 + 31 * 1.25 + 7 * 5) / 1_000_000)
        assert usage[1].cached and usage[1].cost_usd == 0


def test_exact_cache_is_separate_even_when_model_names_are_equal():
    request = dict(model="same", system="s", user="u", schema_name="Answer")
    assert make_cache_key(provider="anthropic", **request) != make_cache_key(
        provider="openai", **request
    )


@pytest.mark.parametrize(
    "schema,raw",
    [
        (JobFitDecisionEvaluation, json.dumps(decision_response())),
    ],
)
def test_fit_annotations_and_truncated_suffix_never_trigger_another_call(
    isolated_db, monkeypatch, schema, raw
):
    provider, calls = fake_provider(monkeypatch, raw=raw, stop="max_tokens")
    result = provider.complete_json(
        system="s", user="u", schema=schema, purpose="job_fit_assessment"
    )
    assert result.levels == (4, 4, 3)
    assert result.original_text == raw
    provider.complete_json(system="s", user="u", schema=schema, purpose="job_fit_assessment")
    assert len(calls) == 1
    with session_scope() as session:
        assert session.scalar(select(LLMCache)).response == raw


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "pause_turn", "tool_use"])
def test_nonfit_incomplete_outputs_record_cost_but_are_not_cached_or_retried(
    isolated_db, monkeypatch, stop
):
    provider, calls = fake_provider(monkeypatch, stop=stop)
    with pytest.raises(LLMValidationError):
        provider.complete_json(system="s", user="u", schema=Answer, purpose="document_review")
    assert len(calls) == 1
    with session_scope() as session:
        assert not list(session.scalars(select(LLMCache)))
        assert len(list(session.scalars(select(LLMUsage)))) == 1


def test_cache_hit_telemetry_error_does_not_buy_a_second_completion(isolated_db, monkeypatch):
    provider, calls = fake_provider(monkeypatch)
    provider.complete_json(system="s", user="u", schema=Answer, purpose="document_review")

    def fail(*args, **kwargs):
        raise RuntimeError("telemetry failure")

    monkeypatch.setattr(provider, "_usage_writer", fail)
    assert (
        provider.complete_json(
            system="s", user="u", schema=Answer, purpose="document_review"
        ).answer
        == "Claude"
    )
    assert len(calls) == 1


def test_refresh_and_no_cache_keep_the_same_explicit_semantics(isolated_db, monkeypatch):
    provider, calls = fake_provider(monkeypatch)
    for options in [{}, {"refresh_cache": True}, {"use_cache": False}]:
        provider.complete_json(
            system="s", user="u", schema=Answer, purpose="document_review", **options
        )
    assert len(calls) == 3
    with session_scope() as session:
        assert len(list(session.scalars(select(LLMCache)))) == 1


def test_factory_and_provider_capture_settings_without_authenticating(_anthropic_test_routes):
    first = get_llm_provider("claude", model="claude-haiku-4-5")
    assert first.name == "anthropic" and first._client is None
    change_call(_anthropic_test_routes, "job_offer_extraction", model="claude-sonnet-4-5")
    assert first.cheap_model == "claude-haiku-4-5"
    assert (
        get_llm_provider("anthropic", model="claude-sonnet-4-5").cheap_model == "claude-sonnet-4-5"
    )


def test_claude_missing_key_fails_before_any_network_client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()._client_lazy()


def test_legacy_openai_hash_is_preserved_and_claude_alias_is_normalized():
    args = dict(model="same", system="s", user="u", schema_name="Answer")
    assert make_cache_key(**args) == make_cache_key(provider="openai", **args)
    assert make_cache_key(provider="anthropic", **args) == make_cache_key(provider="claude", **args)
    assert make_cache_key(provider="anthropic", **args) != make_cache_key(**args)


def test_claude_known_snapshot_pricing_accounts_for_read_and_write():
    assert estimate_cost_usd(
        "claude-sonnet-4-6-20260217", 100, 10, cached_prompt_tokens=20, cache_write_prompt_tokens=30
    ) == pytest.approx((50 * 3 + 20 * 0.3 + 30 * 3.75 + 10 * 15) / 1_000_000)


@pytest.mark.parametrize(
    "model,input_price,output_price,multiplier",
    [
        ("claude-opus-5-5", 4, 20, 0.05),
        ("claude-sonnet-5-5-20260928", 2, 10, 0.05),
        ("claude-fable-5-1", 10, 50, 0.025),
        ("claude-mythos-5-1", 10, 50, 0.025),
    ],
)
def test_model_specific_cache_discount_is_used(model, input_price, output_price, multiplier):
    cost = estimate_cost_usd(model, 100, 10, cached_prompt_tokens=20, cache_write_prompt_tokens=30)
    assert cost == pytest.approx(
        (
            50 * input_price
            + 20 * input_price * multiplier
            + 30 * input_price * 1.25
            + 10 * output_price
        )
        / 1_000_000
    )


@pytest.mark.parametrize(
    "prompt_tokens,input_price,output_price",
    [
        (100000, 0.10, 0.50),
        (100001, 0.50, 2.50),
    ],
)
def test_haiku_prompt_length_tier_includes_cached_input(prompt_tokens, input_price, output_price):
    cost = estimate_cost_usd(
        "claude-haiku-5-5",
        prompt_tokens,
        10,
        cached_prompt_tokens=80000,
        cache_write_prompt_tokens=10000,
    )
    assert cost == pytest.approx(
        (
            (prompt_tokens - 90000) * input_price
            + 80000 * input_price * 0.1
            + 10000 * input_price * 1.25
            + 10 * output_price
        )
        / 1_000_000
    )


def test_claude_client_disables_sdk_retry_multiplication(monkeypatch):
    captured = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("anthropic.Anthropic", fake)
    AnthropicProvider()._client_lazy()
    assert captured == {"api_key": "test-key", "max_retries": 0}
