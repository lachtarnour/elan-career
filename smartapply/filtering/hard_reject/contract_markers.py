"""Contract marker families and occurrence scanning.

This module recognizes marker vocabulary only. Candidate ownership and
modality remain in :mod:`contract_context`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from smartapply.filtering.contract_signals import (
    APPRENTICESHIP_CONTRACT_MARKERS,
    FREELANCE_CONTRACT_MARKERS,
    INDEPENDENT_CONTRACT_MARKERS,
    STAGE_CONTRACT_MARKERS,
)
from smartapply.filtering.hard_reject.contract_patterns import (
    _CANDIDATE_CONTRACT_HEADLINE_RE,
    _CANDIDATE_OPENING_PREFIX_RE,
    _COMPOSITIONAL_MARKER_RE,
    _FIXED_DURATION_CONTRACT_RE,
    _GENERIC_CONTRACT_ROLE_RE,
    _INDEPENDENT_NOUN_SUFFIX_RE,
    _INDEPENDENT_STATUS_MARKER_RE,
    _INDEPENDENT_STATUS_PREFIX_RE,
    _INDEPENDENT_STATUS_SUFFIX_RE,
    _STAGE_PHASE_SUFFIX_RE,
)
from smartapply.filtering.text import norm


class ContractMarkerFamily(str, Enum):
    """Semantic family of one visible incompatible-contract marker."""

    FIXED_TERM = "fixed_term"
    INTERNSHIP = "internship"
    APPRENTICESHIP = "apprenticeship"
    FREELANCE = "freelance"
    INDEPENDENT = "independent"
    CUSTOM = "custom"


@dataclass(frozen=True)
class CandidateContractSignal:
    """A contract marker locally attached to the advertised candidate."""

    family: ContractMarkerFamily
    marker: str
    raw_marker: str
    field: str


def _enabled_marker_families(
    blocked_contract_types: tuple[str, ...],
    *,
    include_fixed_term: bool,
) -> tuple[set[ContractMarkerFamily], tuple[str, ...]]:
    normalized = tuple(
        marker
        for value in blocked_contract_types
        if (marker := norm(value).strip())
    )
    enabled: set[ContractMarkerFamily] = set()
    known_markers: set[str] = set()

    families = (
        (ContractMarkerFamily.INTERNSHIP, STAGE_CONTRACT_MARKERS),
        (ContractMarkerFamily.APPRENTICESHIP, APPRENTICESHIP_CONTRACT_MARKERS),
        (ContractMarkerFamily.FREELANCE, FREELANCE_CONTRACT_MARKERS),
        (ContractMarkerFamily.INDEPENDENT, INDEPENDENT_CONTRACT_MARKERS),
    )
    for family, aliases in families:
        normalized_aliases = {norm(alias).strip() for alias in aliases}
        known_markers.update(normalized_aliases)
        if set(normalized) & normalized_aliases:
            enabled.add(family)
    if include_fixed_term:
        enabled.add(ContractMarkerFamily.FIXED_TERM)

    custom_markers = tuple(
        marker for marker in normalized if marker not in known_markers
    )
    if custom_markers:
        enabled.add(ContractMarkerFamily.CUSTOM)
    return enabled, custom_markers


def _contract_occurrences(
    clause: str,
    *,
    enabled_families: set[ContractMarkerFamily],
    custom_markers: tuple[str, ...],
    contractor_is_corroborated: bool,
) -> list[tuple[int, int, ContractMarkerFamily, str, str]]:
    occurrences: list[tuple[int, int, ContractMarkerFamily, str, str]] = []
    family_by_group = {
        "fixed_term": ContractMarkerFamily.FIXED_TERM,
        "internship": ContractMarkerFamily.INTERNSHIP,
        "apprenticeship": ContractMarkerFamily.APPRENTICESHIP,
        "freelance": ContractMarkerFamily.FREELANCE,
        "independent": ContractMarkerFamily.INDEPENDENT,
    }

    for match in _COMPOSITIONAL_MARKER_RE.finditer(clause):
        family = family_by_group[match.lastgroup or ""]
        if family not in enabled_families:
            continue
        raw_marker = match.group(0)
        if (
            family is ContractMarkerFamily.INDEPENDENT
            and not _is_independent_contract_status(
                clause,
                start=match.start(),
                end=match.end(),
                raw_marker=raw_marker,
            )
        ):
            continue
        if (
            family is ContractMarkerFamily.INTERNSHIP
            and raw_marker == "stage"
            and _is_process_stage(clause, end=match.end())
        ):
            continue
        occurrences.append(
            (
                match.start(),
                match.end(),
                family,
                _canonical_marker(family, raw_marker),
                raw_marker,
            )
        )

    if ContractMarkerFamily.FIXED_TERM in enabled_families:
        for match in _FIXED_DURATION_CONTRACT_RE.finditer(clause):
            occurrences.append(
                (
                    match.start(),
                    match.end(),
                    ContractMarkerFamily.FIXED_TERM,
                    "cdd",
                    match.group(0),
                )
            )

    if (
        contractor_is_corroborated
        and ContractMarkerFamily.FREELANCE in enabled_families
    ):
        for match in _GENERIC_CONTRACT_ROLE_RE.finditer(clause):
            occurrences.append(
                (
                    match.start(),
                    match.end(),
                    ContractMarkerFamily.FREELANCE,
                    "freelance",
                    match.group(0),
                )
            )

    if ContractMarkerFamily.FREELANCE in enabled_families:
        for match in _CANDIDATE_CONTRACT_HEADLINE_RE.finditer(clause):
            marker_start, marker_end = match.span("marker")
            occurrences.append(
                (
                    marker_start,
                    marker_end,
                    ContractMarkerFamily.FREELANCE,
                    "freelance",
                    match.group("marker"),
                )
            )

    if ContractMarkerFamily.CUSTOM in enabled_families:
        for marker in custom_markers:
            marker_re = re.compile(
                rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])"
            )
            for match in marker_re.finditer(clause):
                occurrences.append(
                    (
                        match.start(),
                        match.end(),
                        ContractMarkerFamily.CUSTOM,
                        marker,
                        match.group(0),
                    )
                )

    unique = {
        (start, end, family, marker, raw_marker): (
            start,
            end,
            family,
            marker,
            raw_marker,
        )
        for start, end, family, marker, raw_marker in occurrences
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item[0],
            item[1],
            item[2] is not ContractMarkerFamily.FIXED_TERM,
        ),
    )


def _is_independent_contract_status(
    clause: str,
    *,
    start: int,
    end: int,
    raw_marker: str,
) -> bool:
    """Distinguish self-employment status from ordinary independence."""

    if _INDEPENDENT_STATUS_MARKER_RE.fullmatch(raw_marker):
        return True
    prefix = clause[max(0, start - 100) : start]
    suffix = clause[end : min(len(clause), end + 80)]
    return bool(
        _INDEPENDENT_STATUS_PREFIX_RE.search(prefix)
        or _INDEPENDENT_STATUS_SUFFIX_RE.match(suffix)
        or (
            _CANDIDATE_OPENING_PREFIX_RE.search(prefix)
            and _INDEPENDENT_NOUN_SUFFIX_RE.match(suffix)
        )
    )


def _is_process_stage(clause: str, *, end: int) -> bool:
    """Return whether English ``stage`` denotes a process phase."""

    suffix = clause[end : min(len(clause), end + 24)]
    return _STAGE_PHASE_SUFFIX_RE.match(suffix) is not None


def _canonical_marker(family: ContractMarkerFamily, raw_marker: str) -> str:
    if family is ContractMarkerFamily.FIXED_TERM:
        return "cdd"
    if family is ContractMarkerFamily.INTERNSHIP:
        return "internship" if raw_marker.startswith("intern") else "stage"
    if family is ContractMarkerFamily.APPRENTICESHIP:
        return "alternance"
    if family is ContractMarkerFamily.FREELANCE:
        return "freelance"
    if family is ContractMarkerFamily.INDEPENDENT:
        return "independant"
    return raw_marker


__all__ = ["CandidateContractSignal", "ContractMarkerFamily"]
