"""LLM provider factory."""

from __future__ import annotations

from smartapply.config import get_llm_settings
from smartapply.llm.provider import LLMProvider


def get_llm_provider(name: str | None = None, *, model: str | None = None, settings=None) -> LLMProvider:
    from smartapply.llm.mock_provider import MockLLMProvider
    from smartapply.llm.openai_provider import OpenAIProvider

    settings = settings or get_llm_settings()
    if name is None:
        from smartapply.llm.routing_provider import RoutingProvider

        return RoutingProvider(settings)
    if not model:
        raise ValueError("Modèle IA explicite requis lors de la création d’un fournisseur.")
    chosen = name.strip().lower()
    if chosen == "openai":
        return OpenAIProvider(settings=settings, model=model)
    if chosen in {"anthropic", "claude"}:
        from smartapply.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings=settings, model=model)
    if chosen == "mock":
        return MockLLMProvider(model=model)
    raise ValueError(f"Unknown LLM provider {chosen!r}")
