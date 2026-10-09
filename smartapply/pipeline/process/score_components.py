"""Ownership rules for the shared ``job_scores.components`` JSON object.

Filter updates preserve independent audit and maintenance namespaces.
"""

from __future__ import annotations

LEGACY_MATCH_COMPONENT_KEYS = frozenset(
    {
        "rule_based",
        "semantic",
        "skills",
        "title",
        "seniority",
        "location",
        "domain",
        "final",
        "cap",
    }
)

FILTER_COMPONENT_KEYS = frozenset(
    {
        "reasons",
        "filter_disposition",
        "hard_reject",
        "rejection_stage",
        "rejection_reasons",
        "rejection_summary",
    }
)


def replace_filter_components(
    previous: dict[str, object],
    current: dict[str, object],
) -> dict[str, object]:
    """Replace active filter state and discard legacy Match values."""

    owned = FILTER_COMPONENT_KEYS | LEGACY_MATCH_COMPONENT_KEYS
    return _replace_owned(previous, current, owned)


def replace_filter_state_components(
    previous: dict[str, object],
    current: dict[str, object],
) -> dict[str, object]:
    """Replace filter metadata while preserving unrelated evidence."""

    return _replace_owned(previous, current, FILTER_COMPONENT_KEYS)


def _replace_owned(
    previous: dict[str, object],
    current: dict[str, object],
    owned: frozenset[str],
) -> dict[str, object]:
    retained = {key: value for key, value in previous.items() if key not in owned}
    return {**retained, **current}


__all__ = [
    "FILTER_COMPONENT_KEYS",
    "LEGACY_MATCH_COMPONENT_KEYS",
    "replace_filter_components",
    "replace_filter_state_components",
]
