"""Normalize labels used when the hiring company is not disclosed."""

from __future__ import annotations

_ANONYMOUS_COMPANY_MARKERS = (
    "anonyme",
    "confidentiel",
    "inconnu",
    "non communiqu",
    "non renseign",
    "not disclosed",
    "not provided",
    "unknown",
)


def is_anonymous_company(name: str | None) -> bool:
    """Return whether ``name`` explicitly means that no company is known."""
    normalized = " ".join((name or "").casefold().replace("’", "'").split())
    return not normalized or any(marker in normalized for marker in _ANONYMOUS_COMPANY_MARKERS)
