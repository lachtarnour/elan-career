"""France Travail structured facts for local filtering."""

from __future__ import annotations

import re
from typing import Any

from smartapply.filtering.facts import FilterFacts
from smartapply.filtering.source_fact_builders.common import (
    _clean_text,
    _coerce_bool,
    _coerce_years,
    _format_years,
)
from smartapply.filtering.text import norm
from smartapply.utils.contracts import normalize_source_contract_type

_FT_EXPERIENCE_DURATION_RE = re.compile(
    r"\b(?P<amount>\d{1,3})\s*"
    r"(?P<unit>an(?:\(s\)|s)?|annee(?:\(s\)|s)?|mois|years?|months?)"
    r"(?=\W|$)",
    re.IGNORECASE,
)

_FT_EXPERIENCE_NOT_REQUIRED = {
    "d",
    "debutant accepte",
    "aucune experience",
    "non exige",
    "non exigee",
}
_FT_EXPERIENCE_PREFERRED = {"s", "souhaite", "souhaitee", "preferred"}
_FT_EXPERIENCE_REQUIRED = {"e", "exige", "exigee", "obligatoire", "required"}

_FT_APPRENTICESHIP_MARKERS = (
    "alternance",
    "apprentissage",
    "professionnalisation",
    "contrat apprentissage",
    "cont. professionnalisation",
)
_FT_INTERNSHIP_MARKERS = ("stage", "stagiaire")
_FT_NON_SALARIED_MARKERS = (
    "emploi non salarie",
    "profession liberale",
    "franchise",
    "profession commerciale",
)
_FT_TEMPORARY_MARKERS = ("interim", "mission interim")
_FT_PART_TIME_MARKERS = ("temps partiel", "mi temps", "mi-temps", "part time", "part-time")
_FT_FULL_TIME_MARKERS = ("temps plein", "plein temps", "full time", "full-time")
_FT_WEEKLY_HOURS_RE = re.compile(r"\b(?P<hours>\d{1,2})(?:[,.]\d+)?\s*h\b")
_FT_WEEKLY_PERIOD_RE = re.compile(
    r"(?:/\s*semaine\b|\b(?:par\s+)?semaine\b|\bhebdomadaire\b|"
    r"/\s*week\b|\bper\s+week\b|\bweekly\b)"
)


def build_francetravail_filter_facts(source_data: dict[str, Any]) -> FilterFacts:
    """Extract reliable France Travail structured fields for local filtering."""

    facts = FilterFacts(source="francetravail")
    _extract_francetravail_experience(facts, source_data)
    _extract_francetravail_contract(facts, source_data)
    _extract_francetravail_location(facts, source_data)
    _extract_francetravail_work_modalities(facts, source_data)
    _extract_francetravail_rome_context(facts, source_data)
    return facts


def _extract_francetravail_experience(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    smart = source_data.get("_smartapply_experience")
    smart_exp = smart if isinstance(smart, dict) else {}
    raw_required = source_data.get("experienceExige")
    label = _clean_text(source_data.get("experienceLibelle"))
    comment = _clean_text(source_data.get("experienceCommentaire"))

    requirement = _clean_text(smart_exp.get("requirement"))
    required = _coerce_bool(smart_exp.get("required"))
    if requirement is None:
        required, requirement = _experience_requirement(raw_required, label)
    elif required is None:
        required = _required_from_requirement(requirement)

    if requirement:
        facts.experience_requirement = requirement
        facts.experience_required = required
        facts.experience_source = "francetravail:_smartapply_experience.requirement"
        facts.facts_used.append(f"experience_requirement:{requirement}")

    if requirement in {"beginner_accepted", "preferred"} or required is False:
        years = _coerce_years(smart_exp.get("min_years"))
        if years is not None and years > 0:
            facts.warnings.append("experience_years_ignored_for_non_required_requirement")
        return

    years_source = "francetravail:_smartapply_experience.min_years"
    years = _coerce_years(smart_exp.get("min_years"))
    if years is None:
        years = _parse_ft_experience_years(label, comment)
        years_source = "francetravail:experienceLibelle"

    if years is None:
        return
    if years > 11:
        facts.warnings.append(f"experience_min_years_untrusted:{_format_years(years)}")
        return

    facts.experience_min_years = years
    facts.experience_required = True if required is None else required
    facts.experience_source = years_source
    facts.facts_used.append(f"experience_min_years:{_format_years(years)}")


def _extract_francetravail_contract(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    type_code = _clean_text(source_data.get("typeContrat"))
    type_label = _clean_text(source_data.get("typeContratLibelle"))
    nature = _clean_text(source_data.get("natureContrat"))
    values = (type_code, type_label, nature)
    text = " ".join(norm(value) for value in values if value)

    contract_type: str | None = None
    source = "francetravail:typeContratLibelle"
    if any(marker in text for marker in _FT_APPRENTICESHIP_MARKERS):
        contract_type = "Apprenticeship"
        source = "francetravail:natureContrat"
    elif any(marker in text for marker in _FT_INTERNSHIP_MARKERS):
        contract_type = "Internship"
    elif any(marker in text for marker in _FT_NON_SALARIED_MARKERS) or norm(type_code) in {
        "cce",
        "fra",
        "lib",
    }:
        contract_type = "Freelance"
        source = "francetravail:natureContrat"
    elif any(marker in text for marker in _FT_TEMPORARY_MARKERS) or norm(type_code) == "mis":
        contract_type = "Temporary"
    elif type_label or nature:
        contract_type = normalize_source_contract_type(type_label or nature)

    if not contract_type:
        return

    facts.structured_contract_type = contract_type
    facts.structured_contract_source = source
    facts.facts_used.append(f"structured_contract_type:{contract_type}")


def _extract_francetravail_location(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    lieu = source_data.get("lieuTravail")
    if not isinstance(lieu, dict):
        return
    location = _clean_text(lieu.get("libelle"))
    if not location:
        return
    facts.structured_location = location
    facts.structured_location_source = "francetravail:lieuTravail.libelle"
    facts.facts_used.append("structured_location:lieuTravail.libelle")


def _extract_francetravail_work_modalities(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    alternance = _coerce_bool(source_data.get("alternance"))
    if alternance is not None:
        facts.structured_alternance = alternance
        facts.facts_used.append(f"structured_alternance:{str(alternance).lower()}")

    raw_label = _clean_text(source_data.get("dureeTravailLibelle"))
    weekly_hours = _weekly_hours(raw_label)
    if weekly_hours is not None and weekly_hours <= 1:
        facts.warnings.append(
            f"structured_work_time_implausible_weekly_hours:{weekly_hours}"
        )

    converted = _clean_text(source_data.get("dureeTravailLibelleConverti"))
    if converted:
        work_time = _normalized_work_time_label(converted) or converted
        facts.structured_work_time = work_time
        facts.structured_work_time_source = "francetravail:dureeTravailLibelleConverti"
        facts.facts_used.append(f"structured_work_time:{work_time}")
        return

    work_time = _work_time_from_raw_label(raw_label)
    if not work_time:
        return
    facts.structured_work_time = work_time
    facts.structured_work_time_source = "francetravail:dureeTravailLibelle"
    facts.facts_used.append(f"structured_work_time:{work_time}")


def _extract_francetravail_rome_context(
    facts: FilterFacts,
    source_data: dict[str, Any],
) -> None:
    facts.structured_rome_code = _clean_text(source_data.get("romeCode"))
    facts.structured_rome_label = _clean_text(source_data.get("romeLibelle"))
    facts.structured_appellation_label = _clean_text(source_data.get("appellationlibelle"))

    if facts.structured_rome_code or facts.structured_rome_label:
        parts = [part for part in (facts.structured_rome_code, facts.structured_rome_label) if part]
        facts.facts_used.append(f"structured_rome:{':'.join(parts)}")
    if facts.structured_appellation_label:
        facts.facts_used.append(f"structured_appellation:{facts.structured_appellation_label}")


def _normalized_work_time_label(value: str | None) -> str | None:
    normalized = norm(value)
    if any(marker in normalized for marker in _FT_PART_TIME_MARKERS):
        return "Temps partiel"
    if any(marker in normalized for marker in _FT_FULL_TIME_MARKERS):
        return "Temps plein"
    return None


def _work_time_from_raw_label(value: str | None) -> str | None:
    normalized_label = _normalized_work_time_label(value)
    if normalized_label:
        return normalized_label
    normalized = norm(value)
    if not normalized:
        return None
    match = _FT_WEEKLY_HOURS_RE.search(normalized)
    if not match:
        return None
    hours = int(match.group("hours"))
    if hours < 35:
        return "Temps partiel"
    return None


def _weekly_hours(value: str | None) -> int | None:
    normalized = norm(value)
    if not normalized or not _FT_WEEKLY_PERIOD_RE.search(normalized):
        return None
    match = _FT_WEEKLY_HOURS_RE.search(normalized)
    if not match:
        return None
    return int(match.group("hours"))


def _experience_requirement(
    raw_value: Any,
    label: str | None,
) -> tuple[bool | None, str | None]:
    raw_norm = norm(raw_value).strip(" .:-")
    label_norm = norm(label)
    haystack = " ".join(part for part in (raw_norm, label_norm) if part)
    if not haystack:
        return None, None

    if raw_norm in _FT_EXPERIENCE_NOT_REQUIRED or any(
        marker in haystack
        for marker in ("debutant accepte", "aucune experience", "sans experience")
    ):
        return False, "beginner_accepted"
    if raw_norm in _FT_EXPERIENCE_PREFERRED or any(
        marker in haystack for marker in ("souhaite", "apprecie", "preferred")
    ):
        return False, "preferred"
    if raw_norm in _FT_EXPERIENCE_REQUIRED or any(
        marker in haystack for marker in ("exige", "obligatoire", "required", "demandee")
    ):
        return True, "required"
    if _FT_EXPERIENCE_DURATION_RE.search(label_norm):
        return True, "required"
    return None, "unspecified"


def _required_from_requirement(requirement: str) -> bool | None:
    normalized = norm(requirement)
    if normalized in {"beginner_accepted", "preferred", "not_required"}:
        return False
    if normalized == "required":
        return True
    return None


def _parse_ft_experience_years(*values: str | None) -> float | None:
    for value in values:
        if not value:
            continue
        match = _FT_EXPERIENCE_DURATION_RE.search(norm(value))
        if not match:
            continue
        amount = int(match.group("amount"))
        unit = match.group("unit")
        if unit.startswith(("mois", "month")):
            return round(amount / 12, 2)
        return float(amount)
    return None
