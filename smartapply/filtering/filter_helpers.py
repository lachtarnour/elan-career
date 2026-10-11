"""Context descriptions for deterministic local filtering."""

from __future__ import annotations

from smartapply.filtering.source_facts import FilterFacts
from smartapply.filtering.text import norm as _norm


def _reason_value(value: str | None) -> str:
    return _norm(value).replace(" ", "_") or "unknown"


def _rome_context_reason(facts: FilterFacts) -> str | None:
    parts = [
        value
        for value in (
            facts.structured_rome_code,
            facts.structured_rome_label,
            facts.structured_appellation_label,
        )
        if value
    ]
    if not parts:
        return None
    return "rome_context:" + ":".join(_reason_value(part) for part in parts)


def _search_context_reason(facts: FilterFacts) -> str | None:
    parts = []
    if facts.structured_search_origin:
        parts.append(f"origin={_reason_value(facts.structured_search_origin)}")
    if facts.structured_search_chips:
        parts.append(f"chips={_reason_value(facts.structured_search_chips)}")
    if not parts:
        return None
    return "search_context:" + ",".join(parts)
