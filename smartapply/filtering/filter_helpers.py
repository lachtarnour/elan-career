"""Shared helpers and signal aliases for local job filtering."""

from __future__ import annotations

import re

import smartapply.filtering.contract_signals as contract_signals
import smartapply.filtering.location_signals as location_signals
import smartapply.filtering.role_signals as role_signals
import smartapply.filtering.seniority as seniority_signals
from smartapply.filtering.source_facts import FilterFacts
from smartapply.filtering.text import norm as _norm
from smartapply.utils.contracts import (
    contract_type_tags,
)

_has_apprenticeship_contract_context = contract_signals.has_apprenticeship_contract_context
_has_cdd_contract_context = contract_signals.has_cdd_contract_context
_has_freelance_contract_context = contract_signals.has_freelance_contract_context
_has_independent_contract_context = contract_signals.has_independent_contract_context
_has_part_time_contract_context = contract_signals.has_part_time_contract_context
_has_research_degree_title = contract_signals.has_research_degree_title
_has_stage_contract_context = contract_signals.has_stage_contract_context
_visible_blocked_contract_marker = contract_signals.visible_blocked_contract_marker

_visible_foreign_location_marker = location_signals.visible_foreign_location_marker

_ANALYTICAL_OWNERSHIP_TOKENS = role_signals.ANALYTICAL_OWNERSHIP_TOKENS
_CORE_DATA_TECH_TOKENS = role_signals.CORE_DATA_TECH_TOKENS
_DATA_AI_ANCHOR_TOKENS = role_signals.DATA_AI_ANCHOR_TOKENS
_DATA_ENGINEERING_PLATFORM_TOKENS = role_signals.DATA_ENGINEERING_PLATFORM_TOKENS
_FINANCE_REPORTING_CONTEXT_TOKENS = role_signals.FINANCE_REPORTING_CONTEXT_TOKENS
_ML_ANALYTICS_SCOPE_TOKENS = role_signals.ML_ANALYTICS_SCOPE_TOKENS
_NEGATED_CORE_DATA_TECH_TOKENS = role_signals.NEGATED_CORE_DATA_TECH_TOKENS
_REPORTING_BI_TOKENS = role_signals.REPORTING_BI_TOKENS
_WEB_ANALYTICS_TRACKING_TOKENS = role_signals.WEB_ANALYTICS_TRACKING_TOKENS

_has_candidate_leadership_responsibility = seniority_signals.has_candidate_leadership_responsibility
_has_hidden_senior_role = seniority_signals.has_hidden_senior_role
_title_seniority_or_management_marker = seniority_signals.title_seniority_or_management_marker

_PRESTATAIRE_CONTEXT_PATTERNS = (
    r"\bmission\s+(?:en\s+)?freelance\b",
    r"\bmission\s+de\s+prestation\b",
    r"\bcontexte\s+de\s+la\s+prestation\b",
    r"\bprestation\s+s\s+inscrit\b",
    r"\btarif\s+journalier\b",
    r"\btjm\b",
    r"\bdate\s+de\s+prochaine\s+disponibilite\b",
    r"\bfreelance\b",
    r"\bfreelancer\b",
    r"\bcontractor\b",
    r"\bportage\s+salarial\b",
    r"\bstatut\s+independant\b",
)
_PRESTATAIRE_SOURCE_MARKERS = (
    "free-work",
    "free work",
    "freelance.com",
    "freelance com",
    "freelancerepublik",
    "freelance-republik",
    "malt",
    "lehibou",
    "comet",
)


def _fact_source_suffix(facts: FilterFacts) -> str:
    source = _norm(facts.source).replace(" ", "_")
    return source or "unknown"


def _format_years(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _contract_for_matching(
    job_contract_type: str | None,
    facts: FilterFacts,
) -> str | None:
    if job_contract_type and contract_type_tags(job_contract_type):
        return job_contract_type
    return facts.structured_contract_type or job_contract_type


def _structured_contract_tags(facts: FilterFacts) -> set[str]:
    tags = contract_type_tags(facts.structured_contract_type)
    return tags


def _prestataire_is_corroborated(
    *,
    title: str,
    description: str,
    company: str,
    application_url: str | None,
) -> bool:
    visible_text = f"{title}\n{description}\n{company}"
    if any(re.search(pattern, visible_text) for pattern in _PRESTATAIRE_CONTEXT_PATTERNS):
        return True
    url_text = _norm(application_url)
    return any(marker in url_text for marker in _PRESTATAIRE_SOURCE_MARKERS)


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
