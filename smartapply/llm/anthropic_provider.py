"""Native Claude provider using the same structured request and cache contract."""

from __future__ import annotations

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from smartapply.llm.anthropic_transport import (
    build_message_params,
    parse_message,
    with_requested_model,
)
from smartapply.llm.provider import LLMError
from smartapply.llm.structured_provider import StructuredProvider


class AnthropicProvider(StructuredProvider):
    name = "anthropic"

    @property
    def smart_model(self):
        return self._configured_model()

    @property
    def cheap_model(self):
        return self._configured_model()

    def _client_lazy(self):
        if self._client is None:
            from anthropic import Anthropic

            if not self._settings.anthropic_api_key:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            self._client = Anthropic(api_key=self._settings.anthropic_api_key, max_retries=0)
        return self._client

    def _call_anthropic(self, **kwargs):
        from anthropic import APIConnectionError, APITimeoutError, RateLimitError

        @retry(
            retry=retry_if_exception_type((APIConnectionError, APITimeoutError, RateLimitError)),
            stop=stop_after_attempt(1 if getattr(self, "_disable_ambiguous_retries", False) else 3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            reraise=True,
        )
        def call():
            client = self._client_lazy()
            # The native SDK requires streaming for large output budgets. One
            # stream is still one evaluation, accumulated before publication.
            with client.messages.stream(**kwargs) as stream:
                try:
                    message = stream.get_final_message()
                    # A clean EOF can omit the terminal SSE event without the
                    # SDK raising. Keep usable fit text, but mark it incomplete.
                    if message.stop_reason is None:
                        return message.model_copy(update={"stop_reason": "stream_interrupted"})
                    return message
                except Exception as exc:
                    # HTTP success has already opened this stream. Never buy a
                    # second completion after an interrupted accepted response.
                    # Retain received text for the shared nonblocking fit reader.
                    try:
                        partial = stream.current_message_snapshot
                    except RuntimeError:
                        partial = None
                    if partial is not None and any(
                        block.type == "text" and block.text.strip() for block in partial.content
                    ):
                        return partial.model_copy(update={"stop_reason": "stream_interrupted"})
                    raise LLMError(
                        "Claude response stream interrupted; no usable text received"
                    ) from exc

        return call()

    def _generate(self, body):
        message = self._call_anthropic(**build_message_params(body))
        return with_requested_model(parse_message(message), body["model"])
