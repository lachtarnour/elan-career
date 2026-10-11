"""Welcome to the Jungle structured facts for local filtering."""

from __future__ import annotations

from typing import Any

from smartapply.filtering.facts import FilterFacts
from smartapply.filtering.source_fact_builders.common import (
    _clean_text,
    _coerce_years,
    _dict,
    _format_years,
)
from smartapply.filtering.text import norm
from smartapply.utils.contracts import normalize_source_contract_type

_WTTJ_REMOTE_VALUES = {
    "fulltime": "remote",
    "full time": "remote",
    "full remote": "remote",
    "remote": "remote",
    "total": "remote",
    "partial": "hybrid",
    "partiel": "hybrid",
    "punctual": "hybrid",
    "occasionnel": "hybrid",
    "occasional": "hybrid",
    "hybrid": "hybrid",
    "hybride": "hybrid",
    "no": "onsite",
    "none": "onsite",
    "onsite": "onsite",
    "not allowed": "onsite",
    "no remote": "onsite",
}


def build_wttj_filter_facts(source_data: dict[str, Any]) -> FilterFacts:
    """Extract conservative Welcome to the Jungle facts for local filtering."""

    facts = FilterFacts(source="welcometothejungle")
    _extract_wttj_experience(facts, source_data)
    _extract_wttj_contract(facts, source_data)
    _extract_wttj_location(facts, source_data)
    _extract_wttj_remote_policy(facts, source_data)
    _extract_wttj_search_context(facts, source_data)
    return facts


def _extract_wttj_experience(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    detail_api = _dict(source_data.get("detail_api"))
    matches_api = _dict(source_data.get("matches_api"))
    min_years = _coerce_years(matches_api.get("experience_min"))
    if min_years is not None:
        facts.experience_min_years = min_years
        facts.experience_required = True
        facts.experience_requirement = "experience_min"
        facts.experience_source = "welcometothejungle:matches_api.experience_min"
        facts.facts_used.append("experience_requirement:experience_min")
        facts.facts_used.append(f"experience_min_years:{_format_years(min_years)}")
        return

    level = _clean_text(
        source_data.get("experience_level")
        or detail_api.get("experience_level")
        or matches_api.get("experience_level")
    )
    if not level:
        return
    facts.experience_requirement = level
    facts.experience_required = True
    facts.experience_source = "welcometothejungle:experience_level"
    facts.facts_used.append(f"experience_requirement:{level}")
    min_years = _wttj_experience_min_years(level)
    if min_years is None:
        return
    facts.experience_min_years = min_years
    facts.facts_used.append(f"experience_min_years:{_format_years(min_years)}")


def _extract_wttj_contract(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    refresh = _dict(source_data.get("_smartapply_wttj_refresh"))
    matches_api = _dict(source_data.get("matches_api"))
    raw_contract = (
        _clean_text(refresh.get("raw_contract_type"))
        if refresh.get("schema_version") == 1 else None
    )
    contract_source = "_smartapply_wttj_refresh.raw_contract_type"
    if not raw_contract:
        raw_contract = _clean_text(matches_api.get("contract_type"))
        contract_source = "matches_api.contract_type"
    if not raw_contract:
        raw_contract = _clean_text(source_data.get("employment_type"))
        contract_source = "employment_type"
    if not raw_contract:
        raw_contract = _clean_text(_dict(source_data.get("detail_api")).get("contract_type"))
        contract_source = "detail_api.contract_type"
    if not raw_contract:
        return

    contract_type = normalize_source_contract_type(raw_contract, source=facts.source)
    if not contract_type:
        return

    facts.structured_contract_type = contract_type
    facts.structured_contract_source = f"welcometothejungle:{contract_source}"
    facts.facts_used.append(f"structured_contract_type:{contract_type}")


def _extract_wttj_location(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    workplace = _clean_text(source_data.get("workplace"))
    if workplace:
        facts.structured_location = _compact_wttj_french_location(workplace)
        facts.structured_location_source = "welcometothejungle:workplace"
        facts.facts_used.append("structured_location:workplace")
        return

    matches_api = _dict(source_data.get("matches_api"))
    office = _dict(matches_api.get("office"))
    city = _clean_text(office.get("city"))
    country = _clean_text(office.get("country_code") or office.get("country"))
    location = ", ".join(part for part in (city, country) if part)
    if not location:
        return
    facts.structured_location = location
    facts.structured_location_source = "welcometothejungle:matches_api.office"
    facts.facts_used.append("structured_location:matches_api.office")


def _extract_wttj_remote_policy(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    matches_api = _dict(source_data.get("matches_api"))
    remote_policy = _normalize_wttj_remote_value(matches_api.get("remote"))
    source = "welcometothejungle:matches_api.remote"
    if not remote_policy:
        remote_policy = _normalize_wttj_remote_value(source_data.get("remote_text"))
        source = "welcometothejungle:remote_text"
    if not remote_policy:
        return
    facts.structured_remote_policy = remote_policy
    facts.structured_remote_source = source
    facts.facts_used.append(f"structured_remote_policy:{remote_policy}")


def _extract_wttj_search_context(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    search = source_data.get("_smartapply_search")
    if not isinstance(search, dict):
        return
    mode = _clean_text(search.get("source_mode"))
    pages = _clean_text(search.get("pages"))
    if mode:
        facts.structured_search_origin = mode
        facts.facts_used.append(f"structured_search_origin:{mode}")
    if pages:
        facts.structured_search_chips = f"pages={pages}"
        facts.facts_used.append(f"structured_search_chips:pages={pages}")


def _normalize_wttj_remote_value(value: Any) -> str | None:
    normalized = norm(value).replace("_", " ")
    if not normalized or normalized == "unknown":
        return None
    if normalized in _WTTJ_REMOTE_VALUES:
        return _WTTJ_REMOTE_VALUES[normalized]
    if "teletravail" in normalized or "remote" in normalized:
        if any(marker in normalized for marker in ("total", "full", "100")):
            return "remote"
        if any(marker in normalized for marker in ("frequent", "occasionnel", "partiel")):
            return "hybrid"
    return None


def _compact_wttj_french_location(value: str) -> str:
    locations: list[str] = []
    for raw_location in value.split(";"):
        parts = [_clean_text(part) for part in raw_location.split(",")]
        parts = [part for part in parts if part]
        if len(parts) >= 3 and norm(parts[-1]) in {"fr", "france"}:
            locations.append(f"{parts[0]}, {parts[-1]}")
        else:
            locations.append(", ".join(parts) or _clean_text(raw_location) or "")
    return "; ".join(location for location in locations if location) or value


def _wttj_experience_min_years(value: str | None) -> float | None:
    normalized = norm(value).replace(" ", "_").upper()
    mapping = {
        "NO_EXPERIENCE": 0.0,
        "LESS_THAN_6_MONTHS": 0.0,
        "LESS_THAN_1_YEAR": 0.0,
        "6_MONTHS_TO_1_YEAR": 0.5,
        "1_TO_2_YEARS": 1.0,
        "2_TO_3_YEARS": 2.0,
        "3_TO_4_YEARS": 3.0,
        "4_TO_5_YEARS": 4.0,
        "5_TO_10_YEARS": 5.0,
        "MORE_THAN_10_YEARS": 10.0,
    }
    return mapping.get(normalized)
