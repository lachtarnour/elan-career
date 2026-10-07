"""Ownership rules for the shared ``job_scores.components`` JSON object.

Each pipeline phase may replace only the keys it owns. Independent audit and
maintenance namespaces must survive later filtering and ranking passes.
"""

from __future__ import annotations

RANKING_COMPONENT_KEYS = frozenset(
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


def replace_ranking_components(
    previous: dict[str, object],
    current: dict[str, object],
) -> dict[str, object]:
    """Replace ranking values and preserve all independent namespaces."""

    return _replace_owned(previous, current, RANKING_COMPONENT_KEYS)


def replace_filter_components(
    previous: dict[str, object],
    current: dict[str, object],
) -> dict[str, object]:
    """Replace active filter state and invalidate stale ranking values."""

    owned = FILTER_COMPONENT_KEYS | RANKING_COMPONENT_KEYS
    return _replace_owned(previous, current, owned)


def replace_filter_state_components(
    previous: dict[str, object],
    current: dict[str, object],
) -> dict[str, object]:
    """Replace filter metadata while preserving an existing historical rank."""

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
    "RANKING_COMPONENT_KEYS",
    "replace_filter_components",
    "replace_filter_state_components",
    "replace_ranking_components",
]
