"""Mock LLM provider — produces deterministic outputs for tests.

Tests inject responses via ``register(purpose, schema_instance)``. If no
response is registered for a purpose, the provider raises ``LLMError`` —
this surfaces missing test fixtures loudly instead of returning silent
defaults.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from smartapply.llm.fit_response import FitEvaluationResponse, read_fit_response
from smartapply.llm.provider import LLMError, LLMProvider
from smartapply.llm.schemas import JobFitDecisionEvaluation, JobFitEvaluation

T = TypeVar("T", bound=BaseModel)
MockResponse = BaseModel | FitEvaluationResponse | dict | str


class MockLLMProvider(LLMProvider):
    name = "mock"

    # Class-level defaults keep the terse test API, while each instance copies
    # them at construction time so later test setup cannot mutate existing mocks.
    _default_registry: dict[str, MockResponse | Callable[..., MockResponse]] = {}

    def __init__(
        self, registry: dict[str, MockResponse | Callable[..., MockResponse]] | None = None
    ):
        self._registry = dict(
            registry if registry is not None else self.__class__._default_registry
        )

    @property
    def smart_model(self) -> str:
        return "mock-smart"

    @property
    def cheap_model(self) -> str:
        return "mock-cheap"

    @classmethod
    def register(cls, purpose: str, response: MockResponse | Callable[..., MockResponse]) -> None:
        cls._default_registry[purpose] = response

    @classmethod
    def clear(cls) -> None:
        cls._default_registry.clear()

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        model: str | None = None,
        temperature: float = 0.2,
        purpose: str = "generic",
        job_id: int | None = None,
        use_cache: bool = True,
        refresh_cache: bool = False,
        max_completion_tokens: int | None = None,
    ) -> T | FitEvaluationResponse:
        if purpose not in self._registry:
            raise LLMError(
                f"No mock response registered for purpose={purpose!r}. "
                f"Call MockLLMProvider.register({purpose!r}, <instance>) first."
            )
        response = self._registry[purpose]
        if callable(response):
            response = response(
                system=system, user=user, schema=schema, purpose=purpose, job_id=job_id
            )
        if schema in {JobFitEvaluation, JobFitDecisionEvaluation}:
            return read_fit_response(response, schema=schema)
        if not isinstance(response, schema):
            raise LLMError(
                f"Registered response type {type(response).__name__} does not "
                f"match expected {schema.__name__}"
            )
        return response  # type: ignore[return-value]
