"""Provider routing helpers shared by business callers and frozen journals."""

from __future__ import annotations

from smartapply.llm_policy import CALL_BY_PURPOSE, CALL_TYPES, CallRoute, resolve_route

__all__ = [
    "CALL_BY_PURPOSE",
    "CALL_TYPES",
    "CallRoute",
    "resolve_route",
    "provider_for",
    "frozen_provider",
]


def provider_for(provider, purpose: str):
    """Keep injected providers compatible while routing application providers."""
    resolver = getattr(provider, "for_purpose", None)
    return resolver(purpose) if resolver else provider


def frozen_provider(
    provider, name: str, model: str, *, purpose: str | None = None,
    reasoning: str | None = None, max_output_tokens: int | None = None,
):
    if name not in {"openai", "anthropic", "mock"} or not model:
        raise ValueError("Configuration IA figée : fournisseur ou modèle manquant pour cet appel.")
    if purpose is not None:
        frozen = getattr(provider, "for_frozen_route", None)
        if frozen:
            return frozen(purpose, name, model, reasoning, max_output_tokens)
    resolver = getattr(provider, "for_request", None)
    return resolver(name, model) if resolver else provider
