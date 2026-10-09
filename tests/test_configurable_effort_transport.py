"""Configured effort reaches native requests and keeps exact caches isolated."""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.models import LLMCache
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.batch import build_batch_request
from smartapply.llm.openai_provider import OpenAIProvider
from smartapply.llm.provider import LLMError
from tests.llm_config_helpers import change_call, configure_calls


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh"])
def test_openai_sdk_serializes_native_reasoning_and_sampling_compatibility(
    isolated_db, monkeypatch, tmp_path, effort
):
    configure_calls(monkeypatch, tmp_path, job_offer_extraction={
        "provider": "openai", "model": "gpt-5.4-mini", "reasoning": effort,
        "max_output_tokens": 6000,
    })
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5.4-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"ok"}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(model="gpt-5.4-mini")
        provider._client = openai.OpenAI(api_key="fixture", http_client=client, max_retries=0)
        kwargs = dict(
            system="unchanged system",
            user="unchanged user",
            schema=Answer,
            purpose="job_offer_extraction",
            effort=effort,
            max_completion_tokens=6000,
        )
        assert provider.complete_json(**kwargs).answer == "ok"
        assert provider.complete_json(**kwargs).answer == "ok"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["reasoning_effort"] == effort
    assert "output_config" not in body
    assert ("temperature" in body) == (effort == "none")
    assert body["response_format"]["type"] == "json_schema"
    assert [message["content"] for message in body["messages"]] == [
        "unchanged system",
        "unchanged user",
    ]


@pytest.mark.parametrize("name", ["openai", "anthropic"])
def test_all_explicit_efforts_have_separate_batch_compatible_cache_keys(
    isolated_db, monkeypatch, tmp_path, name
):
    model = "gpt-5.4-mini" if name == "openai" else "claude-haiku-5-5"
    efforts = ["none", "low", "medium", "high", "xhigh"] if name == "openai" else [
        "low", "medium", "high", "xhigh", "max",
    ]
    path = configure_calls(monkeypatch, tmp_path, job_offer_extraction={
        "provider": name, "model": model, "reasoning": efforts[0],
        "max_output_tokens": 6000,
    })
    provider = OpenAIProvider(model=model) if name == "openai" else AnthropicProvider(model=model)
    calls = []

    def generate(**body):
        calls.append(body)
        if name == "anthropic":
            return {
                "model": body["model"],
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"answer":"ok"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        from types import SimpleNamespace

        return SimpleNamespace(
            model=body["model"],
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"answer":"ok"}', refusal=None),
                )
            ],
        )

    monkeypatch.setattr(provider, f"_call_{name}", generate)
    keys = set()
    for effort in efforts + efforts:
        change_call(path, "job_offer_extraction", reasoning=effort)
        provider._settings = get_llm_settings()
        kwargs = dict(
            system="s", user="u", schema=Answer, purpose="job_offer_extraction",
            effort=effort, max_completion_tokens=6000,
        )
        assert provider.complete_json(**kwargs).answer == "ok"
        request = build_batch_request(
            custom_id="request",
            system="s",
            user="u",
            schema=Answer,
            purpose="job_offer_extraction",
            provider=name,
            model=provider.cheap_model,
            max_completion_tokens=6000,
            effort=effort,
        )
        keys.add(request.cache_key)
        with session_scope() as session:
            assert session.scalar(select(LLMCache).where(LLMCache.cache_key == request.cache_key))
    assert len(calls) == len(keys) == len(efforts)


@pytest.mark.parametrize(
    "name,model,effort",
    [
        ("openai", "gpt-5.4-mini", "max"),
        ("anthropic", "claude-haiku-5-5", "none"),
        ("anthropic", "claude-sonnet-4-6", "xhigh"),
    ],
)
def test_incompatible_effort_fails_before_any_provider_call(
    isolated_db, monkeypatch, tmp_path, name, model, effort
):
    configure_calls(monkeypatch, tmp_path, job_offer_extraction={
        "provider": name, "model": model, "reasoning": "medium",
        "max_output_tokens": 6000,
    })
    provider = OpenAIProvider(model=model) if name == "openai" else AnthropicProvider(model=model)

    def unexpected(**kwargs):
        pytest.fail("An incompatible option must not be sent to a provider")

    monkeypatch.setattr(provider, f"_call_{name}", unexpected)
    with pytest.raises(LLMError, match="Unsupported effort"):
        provider.complete_json(
            system="s", user="u", schema=Answer, model=model,
            purpose="job_offer_extraction", effort=effort, max_completion_tokens=6000,
        )
