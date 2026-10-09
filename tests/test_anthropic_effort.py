"""Native effort, single-stream recovery and exact-cache identity without network."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.models import LLMCache, LLMUsage
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.anthropic_transport import build_message_params
from smartapply.llm.batch import build_batch_request
from smartapply.llm.provider import LLMError, LLMValidationError
from smartapply.llm.request_options import request_cache_extra
from smartapply.llm.schemas import JobFitDecisionEvaluation
from smartapply.llm.structured import request_body
from tests.fit_fixtures import decision_response
from tests.llm_config_helpers import change_call, configure_calls


class Answer(BaseModel):
    answer: str


def _message(raw='{"answer":"ok"}', **updates):
    data = dict(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-haiku-5-5",
        content=[{"type": "text", "text": raw}],
        stop_reason="end_turn",
        stop_sequence=None,
        usage={"input_tokens": 10, "output_tokens": 20},
    )
    return anthropic.types.Message(**(data | updates))


@pytest.mark.parametrize(
    "purpose,role,effort",
    [
        ("job_offer_extraction", "cheap", "high"),
        ("job_fit_assessment", "cheap", "high"),
        ("cv_adaptation", "smart", "medium"),
        ("application_texts", "smart", "medium"),
        ("document_review", "smart", "high"),
        ("motivation_letter_repair", "smart", "medium"),
    ],
)
def test_every_stage_sends_its_effort_and_json_format(
    isolated_db, monkeypatch, tmp_path, purpose, role, effort
):
    model = "claude-haiku-5-5" if role == "cheap" else "claude-sonnet-5-5"
    configure_calls(monkeypatch, tmp_path, **{purpose: {
        "provider": "anthropic", "model": model, "reasoning": effort,
        "max_output_tokens": 32768,
    }})
    provider = AnthropicProvider(model=model)
    calls = []

    def call(**params):
        calls.append(copy.deepcopy(params))
        return _message(model=params["model"])

    monkeypatch.setattr(provider, "_call_anthropic", call)
    assert (
        provider.complete_json(
            system="exact system", user="exact user", schema=Answer, model=model,
            purpose=purpose, effort=effort, max_completion_tokens=32768,
        ).answer
        == "ok"
    )
    assert len(calls) == 1
    params = calls[0]
    assert params["model"] == ("claude-haiku-5-5" if role == "cheap" else "claude-sonnet-5-5")
    assert params["output_config"]["effort"] == effort
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["max_tokens"] == 32768
    assert "temperature" not in params
    assert params["system"][0]["text"] == "exact system"
    assert params["messages"][0]["content"] == "exact user"


def test_effort_is_part_of_cache_and_batch_uses_the_identical_key(
    isolated_db, monkeypatch, tmp_path
):
    path = configure_calls(monkeypatch, tmp_path, job_offer_extraction={
        "provider": "anthropic", "model": "claude-haiku-5-5", "reasoning": "high",
        "max_output_tokens": 32768,
    })
    provider = AnthropicProvider(model="claude-haiku-5-5")
    calls = []
    monkeypatch.setattr(
        provider, "_call_anthropic", lambda **params: calls.append(params) or _message()
    )
    for effort in ("high", "medium", "low", "high", "medium", "low"):
        change_call(path, "job_offer_extraction", reasoning=effort)
        provider._settings = get_llm_settings()
        provider.complete_json(
            system="s", user="u", schema=Answer, purpose="job_offer_extraction",
            effort=effort, max_completion_tokens=32768,
        )
    assert len(calls) == 3
    with session_scope() as session:
        assert len(list(session.scalars(select(LLMCache)))) == 3
    batch = build_batch_request(
        custom_id="extraction-1",
        system="s",
        user="u",
        schema=Answer,
        model=provider.cheap_model,
        purpose="job_offer_extraction",
        max_completion_tokens=32768,
        provider="anthropic",
        effort="high",
    )
    with session_scope() as session:
        cached = session.scalar(select(LLMCache).where(LLMCache.cache_key == batch.cache_key))
        assert cached is not None and cached.response == '{"answer":"ok"}'
    assert build_message_params(batch.body)["output_config"]["effort"] == "high"


def test_frozen_request_without_effort_does_not_invent_one():
    body = request_body(
        system="s",
        user="u",
        schema=Answer,
        model="claude-haiku-5-5",
        temperature=0.2,
        purpose="job_offer_extraction",
        max_completion_tokens=6000,
    )
    assert "effort" not in build_message_params(body)["output_config"]
    assert request_cache_extra(Answer, 0.2, None) == {
        "temperature": 0.2,
        "schema": Answer.model_json_schema(),
    }


def test_sdk_serializes_large_budget_as_one_native_stream(isolated_db, monkeypatch, tmp_path):
    configure_calls(monkeypatch, tmp_path, job_offer_extraction={
        "provider": "anthropic", "model": "claude-haiku-5-5", "reasoning": "high",
        "max_output_tokens": 32768,
    })
    requests = []
    message = _message().model_dump()
    message["content"] = []
    message["stop_reason"] = None
    events = [
        ("message_start", {"message": message}),
        (
            "content_block_start",
            {"index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "reasoning"}},
        ),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": {"type": "text", "text": ""}}),
        (
            "content_block_delta",
            {"index": 1, "delta": {"type": "text_delta", "text": '{"answer":"ok"}'}},
        ),
        ("content_block_stop", {"index": 1}),
        (
            "message_delta",
            {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 30},
            },
        ),
        ("message_stop", {}),
    ]
    sse = "".join(
        f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n" for name, data in events
    )

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as http_client:
        provider = AnthropicProvider(model="claude-haiku-5-5")
        provider._client = anthropic.Anthropic(
            api_key="fixture-key", http_client=http_client, max_retries=0
        )
        result = provider.complete_json(
            system="s", user="u", schema=Answer, purpose="job_offer_extraction",
            effort="high", max_completion_tokens=32768,
        )
    assert result.answer == "ok" and len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/messages"
    body = json.loads(request.content)
    assert body["stream"] and body["max_tokens"] == 32768
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in body
    with session_scope() as session:
        usage = session.scalar(select(LLMUsage))
        assert usage.prompt_tokens == 10 and usage.completion_tokens == 30


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({**decision_response(), "issues": "invalid annotations"}),
        json.dumps(decision_response())[:-1] + ',"neutral_offer_re',
    ],
)
def test_interrupted_stream_preserves_fit_text_cache_and_never_retries(
    isolated_db, monkeypatch, tmp_path, raw
):
    configure_calls(monkeypatch, tmp_path, job_fit_assessment={
        "provider": "anthropic", "model": "claude-haiku-5-5", "reasoning": "high",
        "max_output_tokens": 32768,
    })
    partial = _message(raw)
    calls = []

    class Stream:
        current_message_snapshot = partial

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_final_message(self):
            raise anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            )

    def stream(**params):
        calls.append(params)
        return Stream()

    provider = AnthropicProvider(model="claude-haiku-5-5")
    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    first = provider.complete_json(
        system="s", user="u", schema=JobFitDecisionEvaluation, purpose="job_fit_assessment",
        effort="high", max_completion_tokens=32768,
    )
    second = provider.complete_json(
        system="s", user="u", schema=JobFitDecisionEvaluation, purpose="job_fit_assessment",
        effort="high", max_completion_tokens=32768,
    )
    assert first.levels == second.levels == (4, 4, 3) and len(calls) == 1
    assert any("stream_interrupted" in observation for observation in first.observations)
    assert first.original_text == raw
    with session_scope() as session:
        assert session.scalar(select(LLMCache)).response == raw


def test_accepted_stream_with_no_text_fails_without_a_second_post(
    isolated_db, monkeypatch, tmp_path
):
    configure_calls(monkeypatch, tmp_path, application_texts={
        "provider": "anthropic", "model": "claude-haiku-5-5", "reasoning": "high",
        "max_output_tokens": 32768,
    })
    calls = []

    class Stream:
        current_message_snapshot = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_final_message(self):
            raise anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            )

    def stream(**params):
        calls.append(params)
        return Stream()

    provider = AnthropicProvider(model="claude-haiku-5-5")
    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    with pytest.raises(LLMError, match="no usable text"):
        provider.complete_json(system="s", user="u", schema=Answer,
                               purpose="application_texts", effort="high",
                               max_completion_tokens=32768)
    assert len(calls) == 1


@pytest.mark.parametrize("fit", [True, False])
def test_sdk_eof_without_terminal_reason_keeps_fit_but_rejects_document(
    isolated_db, monkeypatch, tmp_path, fit
):
    purpose = "job_fit_assessment" if fit else "document_review"
    configure_calls(monkeypatch, tmp_path, **{purpose: {
        "provider": "anthropic", "model": "claude-haiku-5-5", "reasoning": "high",
        "max_output_tokens": 32768,
    }})
    raw = json.dumps(decision_response()) if fit else '{"answer":"ok"}'
    requests = []
    message = _message().model_dump()
    message.update(content=[], stop_reason=None)
    events = [
        ("message_start", {"message": message}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": raw}}),
    ]
    sse = "".join(
        f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n" for name, data in events
    )

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as http_client:
        provider = AnthropicProvider(model="claude-haiku-5-5")
        provider._client = anthropic.Anthropic(
            api_key="fixture-key", http_client=http_client, max_retries=0
        )
        kwargs = dict(system="s", user="u", schema=JobFitDecisionEvaluation if fit else Answer,
                      purpose="job_fit_assessment" if fit else "document_review",
                      effort="high", max_completion_tokens=32768)
        if fit:
            result = provider.complete_json(**kwargs)
            assert result.levels == (4, 4, 3)
            assert result.original_text == raw
            assert any("stream_interrupted" in warning for warning in result.observations)
            assert provider.complete_json(**kwargs).levels == result.levels
        else:
            with pytest.raises(LLMValidationError, match="Incomplete"):
                provider.complete_json(**kwargs)
    assert len(requests) == 1
    with session_scope() as session:
        cached = session.scalar(select(LLMCache))
        assert (cached is not None) == fit
        if fit:
            assert cached.response == raw
