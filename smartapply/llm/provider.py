"""LLM provider ABC and factory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

if TYPE_CHECKING:
    from smartapply.llm.fit_response import FitDecisionResponse


class LLMProvider(ABC):
    """Abstract LLM provider with a single structured-output method.

    Implementations validate the returned data before returning it. Fit evaluation
    is special: its generation schema guides the model, while the response reader
    requires only the four fit decisions and preserves imperfect annotations.
    """

    name: str = ""

    @abstractmethod
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
        effort: str | None = "auto",
    ) -> T | FitDecisionResponse: ...

    @property
    @abstractmethod
    def smart_model(self) -> str: ...

    @property
    @abstractmethod
    def cheap_model(self) -> str: ...


class LLMError(RuntimeError):
    """Base error for LLM provider failures."""


class LLMValidationError(LLMError):
    """Raised when the model output does not match the schema."""
