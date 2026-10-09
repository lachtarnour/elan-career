"""Anthropic Message Batches behind the shared durable analysis journal.

Anthropic submits request bodies directly and has no submission metadata lookup.
Wire IDs include the wave UUID; after an uncertain POST, recovery only identifies
a batch whose complete result-ID set matches that wave. Pending unknown batches
remain under attention, and are never submitted a second time.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from smartapply.config import get_llm_settings
from smartapply.llm.anthropic_transport import build_message_params, parse_message
from smartapply.llm.batch import (
    BatchRequest,
    BatchResult,
    BatchSnapshot,
    _as_mapping,
    _nonnegative_int,
    encode_requests,
)
from smartapply.llm.provider import LLMError

MAX_ANTHROPIC_BATCH_BYTES = 256_000_000
MAX_ANTHROPIC_BATCH_REQUESTS = 100_000
_WIRE_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


def _timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LLMError("Invalid Anthropic batch timestamp") from exc
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise LLMError("Anthropic batch timestamp has no timezone")
        value = int(value.timestamp())
    return _nonnegative_int(value)


def _intent_prefix(metadata: Mapping[str, str]) -> str:
    wave = metadata.get("elan_wave_id", "").replace("-", "")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", wave):
        raise ValueError("Anthropic batch submission needs its saved wave UUID")
    return wave.lower() + "_"


def _input_reference(requests: Iterable[BatchRequest]) -> str:
    return "anthropic-input:" + hashlib.sha256(encode_requests(requests)).hexdigest()


def _snapshot(raw: Any) -> BatchSnapshot:
    value = _as_mapping(raw)
    batch_id = value.get("id")
    status = value.get("processing_status")
    if not isinstance(batch_id, str) or not batch_id:
        raise LLMError("Anthropic batch response has no batch ID")
    if status not in {"in_progress", "canceling", "ended"}:
        raise LLMError(f"Unknown Anthropic batch status: {status!r}")
    raw_counts = _as_mapping(value.get("request_counts"))
    counts = {
        name: _nonnegative_int(raw_counts.get(name))
        for name in ("processing", "succeeded", "errored", "canceled", "expired")
    }
    if status == "ended" and counts["processing"]:
        raise LLMError("An ended Anthropic batch still has processing requests")
    normalized = "in_progress" if status == "in_progress" else "cancelling"
    if status == "ended":
        normalized = (
            "cancelled" if counts["canceled"] else
            "expired" if counts["expired"] else "completed"
        )
    ended_at = _timestamp(value.get("ended_at"))
    return BatchSnapshot(
        id=batch_id,
        status=normalized,
        input_file_id=f"anthropic-input:{batch_id}",
        output_file_id=f"anthropic-results:{batch_id}" if status == "ended" else None,
        total=sum(counts.values()),
        completed=counts["succeeded"],
        failed=counts["errored"] + counts["canceled"] + counts["expired"],
        created_at=_timestamp(value.get("created_at")),
        expires_at=_timestamp(value.get("expires_at")),
        cancelling_at=_timestamp(value.get("cancel_initiated_at")),
        completed_at=ended_at if normalized == "completed" else None,
        expired_at=ended_at if normalized == "expired" else None,
        cancelled_at=ended_at if normalized == "cancelled" else None,
        provider="anthropic",
        provider_status=status,
    )


def _result(raw: Any, *, strip_prefix: bool) -> BatchResult:
    value = _as_mapping(raw)
    custom_id = value.get("custom_id")
    if not isinstance(custom_id, str) or not _WIRE_ID.fullmatch(custom_id):
        raise LLMError("Anthropic batch result has an invalid custom_id")
    if strip_prefix:
        prefix, separator, original = custom_id.partition("_")
        if not separator or not re.fullmatch(r"[a-f0-9]{32}", prefix) or not original:
            raise LLMError("Anthropic batch result does not belong to an Élan wave")
        custom_id = original
    result = _as_mapping(value.get("result"))
    kind = result.get("type")
    if kind in {"errored", "canceled", "expired"}:
        error = _as_mapping(result.get("error"))
        # Anthropic's errored result contains an ErrorResponse envelope.
        details = _as_mapping(error.get("error")) if "error" in error else error
        return BatchResult(
            custom_id=custom_id,
            error_code=str(details.get("type") or f"batch_{kind}"),
            error_message=str(details.get("message") or f"Anthropic batch request {kind}"),
        )
    if kind != "succeeded":
        raise LLMError(f"Unknown Anthropic batch result type: {kind!r}")
    message = _as_mapping(result.get("message"))
    parsed = parse_message(message)
    common = {
        "custom_id": custom_id,
        "content": parsed.content,
        "prompt_tokens": parsed.prompt_tokens,
        "completion_tokens": parsed.completion_tokens,
        "cached_prompt_tokens": parsed.cached_prompt_tokens,
        "cache_write_prompt_tokens": parsed.cache_write_prompt_tokens,
        "response_id": message.get("id"),
        "model": message.get("model"),
    }
    if parsed.refusal:
        return BatchResult(**common, error_code="refusal", error_message=str(parsed.refusal))
    if parsed.finish_reason != "stop":
        return BatchResult(
            **common,
            error_code="incomplete_output",
            error_message=f"Completion ended with {parsed.finish_reason!r}",
        )
    if not parsed.content.strip():
        return BatchResult(**common, error_code="empty_output", error_message="No JSON content")
    return BatchResult(**common)


def parse_anthropic_results(
    records: Iterable[Any], *, strip_prefix: bool = True, expected_prefix: str | None = None
):
    """Preserve unordered outcomes and reject duplicate IDs before importing."""
    results: dict[str, BatchResult] = {}
    for record in records:
        if expected_prefix is not None and not str(
            _as_mapping(record).get("custom_id", "")
        ).startswith(expected_prefix):
            raise LLMError("Anthropic result belongs to another wave")
        result = _result(record, strip_prefix=strip_prefix)
        if result.custom_id in results:
            raise LLMError(f"Duplicate custom_id in Anthropic batch output: {result.custom_id}")
        results[result.custom_id] = result
    return results


class AnthropicBatchTransport:
    provider = "anthropic"
    requires_journal = True

    def __init__(self, client: Any = None, *, settings=None):
        self._client = client
        self._settings = settings

    def _client_lazy(self):
        if self._client is None:
            from anthropic import Anthropic

            key = (self._settings or get_llm_settings()).anthropic_api_key
            if not key:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            self._client = Anthropic(api_key=key, timeout=30.0, max_retries=0)
        return self._client

    def upload(self, content: bytes) -> str:
        """Validate the journal locally; Anthropic has no Batch file upload."""
        records = [json.loads(line) for line in content.decode("utf-8").splitlines()]
        for record in records:
            if len(record["custom_id"]) > 31 or not _WIRE_ID.fullmatch(record["custom_id"]):
                raise ValueError("Anthropic journal IDs must leave room for the wave UUID")
            build_message_params(record["body"])
        if not records:
            raise ValueError("Cannot submit an empty batch")
        # Authentication/configuration errors are established before the
        # journal transitions to an uncertain provider POST.
        self._client_lazy()
        return "anthropic-input:" + hashlib.sha256(content).hexdigest()

    def create(
        self,
        input_file_id: str,
        metadata: dict[str, str],
        *,
        requests: list[BatchRequest],
    ) -> BatchSnapshot:
        if input_file_id != _input_reference(requests):
            raise ValueError("Anthropic request journal changed after preparation")
        prefix = _intent_prefix(metadata)
        wire = [
            {"custom_id": prefix + request.custom_id, "params": build_message_params(request.body)}
            for request in requests
        ]
        if any(not _WIRE_ID.fullmatch(record["custom_id"]) for record in wire):
            raise ValueError("Invalid Anthropic Batch custom_id")
        size = len(json.dumps({"requests": wire}, ensure_ascii=False).encode("utf-8"))
        if len(wire) > MAX_ANTHROPIC_BATCH_REQUESTS or size > MAX_ANTHROPIC_BATCH_BYTES:
            raise ValueError("Anthropic Batch exceeds its request or byte limit")
        raw = self._client_lazy().with_options(max_retries=0).messages.batches.create(requests=wire)
        return _snapshot(raw)

    def retrieve(self, batch_id: str) -> BatchSnapshot:
        return _snapshot(self._client_lazy().messages.batches.retrieve(batch_id))

    def recover(
        self,
        metadata: dict[str, str],
        *,
        requests: list[BatchRequest],
        created_after: datetime,
    ) -> list[BatchSnapshot]:
        prefix = _intent_prefix(metadata)
        expected = {prefix + request.custom_id for request in requests}
        if not expected:
            raise ValueError("Anthropic recovery needs its saved requests")
        earliest = (
            created_after.replace(tzinfo=timezone.utc)
            if created_after.tzinfo is None else created_after
        )
        matches = []
        # The SDK page iterator follows pagination. Pending batches have no
        # readable request IDs, so cannot establish this submission's identity.
        for raw in self._client_lazy().messages.batches.list(limit=100):
            snapshot = _snapshot(raw)
            if snapshot.created_at is None or snapshot.created_at < int(earliest.timestamp()) - 60:
                continue
            if not snapshot.terminal or snapshot.total != len(expected):
                continue
            records = list(self._client_lazy().messages.batches.results(snapshot.id))
            ids = [_as_mapping(record).get("custom_id") for record in records]
            if len(ids) == len(expected) and set(ids) == expected:
                matches.append(snapshot)
        return matches

    def download(self, file_id: str, *, metadata: dict[str, str]) -> dict[str, BatchResult]:
        prefix = "anthropic-results:"
        if not file_id.startswith(prefix) or not file_id[len(prefix):]:
            raise ValueError("Invalid Anthropic results reference")
        return parse_anthropic_results(
            self._client_lazy().messages.batches.results(file_id[len(prefix):]),
            expected_prefix=_intent_prefix(metadata),
        )

    def cancel(self, batch_id: str) -> BatchSnapshot:
        return _snapshot(self._client_lazy().messages.batches.cancel(batch_id))
