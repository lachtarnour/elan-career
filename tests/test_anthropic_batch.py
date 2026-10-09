"""Native Claude Batch contracts without credentials or network calls."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from smartapply.llm.anthropic_batch import (
    AnthropicBatchTransport,
    _snapshot,
    parse_anthropic_results,
)
from smartapply.llm.batch import build_batch_request, encode_requests, get_batch_transport
from smartapply.llm.cache import make_cache_key
from smartapply.llm.provider import LLMError, LLMValidationError
from tests.test_llm_batch import Extraction

WAVE = "83c42b40-77ad-4dc8-8bae-a2fa9a5ec93c"
PREFIX = WAVE.replace("-", "") + "_"
METADATA = {"elan_run_id": "run", "elan_wave_id": WAVE}


def request(custom_id="extraction-42"):
    return build_batch_request(
        custom_id=custom_id, system="Extraire seulement les faits.",
        user="Ingénieur IA", schema=Extraction, model="claude-haiku-4-5",
        max_completion_tokens=6000, purpose="job_offer_analysis", provider="anthropic",
    )


def snapshot(**changes):
    return {
        "id": "msgbatch-42", "processing_status": "in_progress",
        "request_counts": {"processing": 2, "succeeded": 0, "errored": 0,
                           "canceled": 0, "expired": 0},
        "created_at": "2026-10-08T20:00:00Z", "expires_at": "2026-10-09T20:00:00Z",
        "ended_at": None, "cancel_initiated_at": None, **changes,
    }


def success(custom_id=PREFIX + "extraction-42", *, stop_reason="end_turn", content=None):
    return {
        "custom_id": custom_id,
        "result": {"type": "succeeded", "message": {
            "id": "msg-42", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": content or '{"role":"AI","skills":[]}'}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 20, "output_tokens": 50,
                      "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10},
        }},
    }


def test_claude_batch_cache_matches_immediate_and_isolated_from_openai():
    built = request()
    params = dict(model="claude-haiku-4-5", system="Extraire seulement les faits.",
                  user="Ingénieur IA", schema_name="Extraction",
                  extra={"temperature": 0.2, "schema": Extraction.model_json_schema()})
    assert built.cache_key == make_cache_key(**params, provider="anthropic")
    assert built.cache_key != make_cache_key(**params, provider="openai")


def test_submission_translates_the_saved_journal_without_changing_prompts():
    client = MagicMock()
    client.with_options.return_value.messages.batches.create.return_value = snapshot()
    transport = AnthropicBatchTransport(client)
    built = request()
    before = built.body.copy()
    input_reference = transport.upload(encode_requests([built]))
    result = transport.create(input_reference, METADATA, requests=[built])
    assert result.provider == "anthropic" and result.status == "in_progress"
    assert built.body == before
    wire = client.with_options.return_value.messages.batches.create.call_args.kwargs["requests"]
    assert wire[0]["custom_id"] == PREFIX + "extraction-42"
    assert wire[0]["params"]["system"][0]["text"] == built.body["messages"][0]["content"]
    assert wire[0]["params"]["messages"][0]["content"] == built.body["messages"][1]["content"]
    assert wire[0]["params"]["max_tokens"] == 6000
    assert wire[0]["params"]["output_config"]["format"]["type"] == "json_schema"
    client.with_options.assert_called_once_with(max_retries=0)
    client.files.create.assert_not_called()


def test_journal_integrity_and_native_limits_fail_before_provider_call():
    client = MagicMock()
    transport = AnthropicBatchTransport(client)
    with pytest.raises(ValueError, match="changed"):
        transport.create("different-input", METADATA, requests=[request()])
    with pytest.raises(ValueError, match="room"):
        transport.upload(encode_requests([request("x" * 32)]))
    client.messages.batches.create.assert_not_called()


def test_unknown_post_outcome_is_never_retried():
    client = MagicMock()
    client.with_options.return_value.messages.batches.create.side_effect = TimeoutError("unknown")
    transport = AnthropicBatchTransport(client)
    built = request()
    reference = transport.upload(encode_requests([built]))
    with pytest.raises(TimeoutError):
        transport.create(reference, METADATA, requests=[built])
    assert client.with_options.return_value.messages.batches.create.call_count == 1


@pytest.mark.parametrize("kind", ["completed", "cancelled", "expired"])
def test_terminal_states_preserve_successes_failures_and_provider_status(kind):
    counts = {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0}
    counts[{"completed": "errored", "cancelled": "canceled", "expired": "expired"}[kind]] = 1
    result = _snapshot(snapshot(processing_status="ended", request_counts=counts,
                                ended_at="2026-10-08T21:00:00Z"))
    assert result.terminal and result.status == kind and result.provider_status == "ended"
    assert result.total == 2 and result.completed == 1 and result.failed == 1
    assert result.output_file_id == "anthropic-results:msgbatch-42"


def test_cancelling_and_invalid_states_are_never_presented_as_complete():
    assert _snapshot(snapshot(processing_status="canceling")).status == "cancelling"
    for changes in [{"processing_status": "unknown"}, {"processing_status": "ended"},
                    {"request_counts": {"processing": True}}, {"id": ""}]:
        with pytest.raises(LLMError):
            _snapshot(snapshot(**changes))


def test_unordered_results_normalize_all_billable_token_types():
    results = parse_anthropic_results([
        success(PREFIX + "extraction-99"), success(),
    ], expected_prefix=PREFIX)
    assert set(results) == {"extraction-42", "extraction-99"}
    result = results["extraction-42"]
    assert result.validate(Extraction).role == "AI"
    assert result.prompt_tokens == 110 and result.completion_tokens == 50
    assert result.cached_prompt_tokens == 80 and result.cache_write_prompt_tokens == 10


@pytest.mark.parametrize("kind", ["errored", "canceled", "expired"])
def test_individual_failed_results_are_not_successes(kind):
    value = {"custom_id": PREFIX + "extraction-42", "result": {"type": kind}}
    if kind == "errored":
        value["result"]["error"] = {"type": "error", "error": {
            "type": "invalid_request_error", "message": "bad request",
        }}
    result = parse_anthropic_results([value])["extraction-42"]
    assert not result.successful
    with pytest.raises(LLMValidationError):
        result.validate(Extraction)


@pytest.mark.parametrize(("reason", "code"), [("max_tokens", "incomplete_output"),
                                                ("refusal", "refusal")])
def test_truncated_and_refused_outputs_keep_content_and_usage(reason, code):
    result = parse_anthropic_results([success(stop_reason=reason)])["extraction-42"]
    assert result.error_code == code and result.content and result.prompt_tokens == 110


def test_duplicate_foreign_and_unknown_outcomes_are_not_imported():
    with pytest.raises(LLMError, match="Duplicate"):
        parse_anthropic_results([success(), success()])
    with pytest.raises(LLMError, match="another wave"):
        parse_anthropic_results([success("0" * 32 + "_extraction-42")], expected_prefix=PREFIX)
    with pytest.raises(LLMError, match="Unknown"):
        parse_anthropic_results([{"custom_id": PREFIX + "extraction-42",
                                 "result": {"type": "unexpected"}}])


def test_recovery_waits_for_results_and_requires_the_entire_wave_id_set():
    client = MagicMock()
    ended_counts = {"processing": 0, "succeeded": 2}
    client.messages.batches.list.return_value = [
        snapshot(id="pending"),
        snapshot(id="wrong", processing_status="ended", request_counts=ended_counts),
        snapshot(id="match-1", processing_status="ended", request_counts=ended_counts),
        snapshot(id="match-2", processing_status="ended", request_counts=ended_counts),
    ]
    outputs = {
        "wrong": [success(), success("0" * 32 + "_extraction-99")],
        "match-1": [success(), success(PREFIX + "extraction-99")],
        "match-2": [success(PREFIX + "extraction-99"), success()],
    }
    client.messages.batches.results.side_effect = outputs.__getitem__
    transport = AnthropicBatchTransport(client)
    matches = transport.recover(
        METADATA, requests=[request(), request("extraction-99")],
        created_after=datetime(2026, 10, 8, 20, tzinfo=timezone.utc),
    )
    assert [match.id for match in matches] == ["match-1", "match-2"]
    assert all(call.args != ("pending",) for call in client.messages.batches.results.call_args_list)
    client.messages.batches.create.assert_not_called()


def test_factory_uses_the_explicit_saved_provider():
    assert get_batch_transport("anthropic").provider == "anthropic"
    assert get_batch_transport("openai").provider == "openai"
    with pytest.raises(ValueError, match="OpenAI ou Anthropic"):
        get_batch_transport("mock")
