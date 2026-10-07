"""Context model for visible contract wording.

Contract words are decisive only when they describe the advertised candidate's
own mandatory arrangement.  This module removes mentions about past
experience, third parties, optional schedules, and experience-level menus
before the contract gate applies its strict contract lexicon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from smartapply.filtering.hard_reject.contract_markers import (
    CandidateContractSignal,
    ContractMarkerFamily,
    _contract_occurrences,
    _enabled_marker_families,
)
from smartapply.filtering.hard_reject.contract_patterns import (
    _ALTERNATIVE_CONNECTOR_RE,
    _ATS_CONTRACT_VALUE_PREFIX_RE,
    _BLOCKED_TERM_RE,
    _CANDIDATE_ACTIVITY_PREFIX_RE,
    _CANDIDATE_CONTRACT_FRAME_GAP_RE,
    _CANDIDATE_CONTRACT_FRAME_RE,
    _CANDIDATE_OFFER_FRAME_RE,
    _CANDIDATE_OPENING_PREFIX_RE,
    _CANDIDATE_SEARCH_PREFIX_RE,
    _CLAUSE_SPLIT_RE,
    _COMPATIBLE_LEVEL_RE,
    _CONTRACT_ALTERNATIVE_PREFIX_RE,
    _CONTRACT_DETAIL_SUFFIX_RE,
    _CONTRACT_LABEL_PREFIX_RE,
    _COORDINATED_ATTRIBUTE_PREFIX_RE,
    _COORDINATED_CANDIDATE_ACTIVITY_RE,
    _COORDINATED_OFFER_ACTION_RE,
    _CURRENT_OFFER_ASSERTION_PREFIX_RE,
    _CURRENT_OFFER_REFERENCE_PREFIX_RE,
    _CURRENT_ROLE_CONTRACT_PREFIX_RE,
    _DURATION_SUFFIX_RE,
    _EXCLUSIVE_CONTRACT_SUFFIX_RE,
    _FIXED_TERM_ROLE_SUFFIX_RE,
    _FULL_TIME_RE,
    _GENERIC_LISTING_RE,
    _HISTORICAL_MARKER_SUFFIX_RE,
    _HISTORICAL_RE,
    _HISTORICAL_TOKEN_RE,
    _JOB_SEARCH_BOILERPLATE_RE,
    _LEVEL_HEADING_RE,
    _LEVEL_MENU_FRAME_RE,
    _MANDATORY_RE,
    _NEGATED_MARKER_PREFIX_RE,
    _NEGATED_MARKER_SUFFIX_RE,
    _NON_CONTRACT_HISTORICAL_PATTERNS,
    _NON_CONTRACT_ROTATION_PATTERNS,
    _NON_CONTRACT_SCIENTIFIC_PATTERNS,
    _NON_CONTRACT_STAGE_TITLE_PATTERNS,
    _NON_HEADLINE_STATUS_PREFIX_RE,
    _OFFER_ACTION_GAP_RE,
    _OFFER_ACTION_RE,
    _OFFER_ATTRIBUTE_PREFIX_RE,
    _OFFER_NOUN_SUFFIX_RE,
    _OFFER_STATUS_SUFFIX_RE,
    _OPPORTUNITY_FOR_CANDIDATE_PREFIX_RE,
    _OPTIONAL_MARKER_CONTEXT_RE,
    _OPTIONAL_RE,
    _ORGANIZATION_INDEPENDENCE_PREFIX_RE,
    _ORGANIZATION_INDEPENDENCE_SUFFIX_RE,
    _PART_TIME_RE,
    _PAST_CANDIDATE_ACTION_RE,
    _PERMANENT_TERM_RE,
    _PREDICATE_BOUNDARY_RE,
    _PROGRAM_RESPONSIBILITY_SUFFIX_RE,
    _RECRUITMENT_CONTRACT_PREFIX_RE,
    _RECRUITMENT_SERVICE_CONTEXT_RE,
    _SEASONAL_CONTRACT_SUFFIX_RE,
    _SELF_IDENTIFICATION_PREFIX_RE,
    _SELF_IDENTIFICATION_SUFFIX_RE,
    _SHORT_HEADING_PREFIX_RE,
    _SHORT_HEADING_SUFFIX_RE,
    _SHORT_ROLE_CONTRACT_PREFIX_RE,
    _THIRD_PARTY_DUTY_RE,
    _THIRD_PARTY_DUTY_VERB_RE,
    _THIRD_PARTY_ENTITY_RE,
    _THIRD_PARTY_MANAGER_SUFFIX_RE,
    _THIRD_PARTY_RELATION_RE,
    _THIRD_PARTY_ROLE_TERM_RE,
)
from smartapply.filtering.text import norm


class ClauseActor(str, Enum):
    CANDIDATE = "candidate"
    THIRD_PARTY = "third_party"
    UNKNOWN = "unknown"


class ClauseModality(str, Enum):
    MANDATORY_OR_UNKNOWN = "mandatory_or_unknown"
    HISTORICAL = "historical"
    OPTIONAL = "optional"
    LEVEL_ALTERNATIVE = "level_alternative"


@dataclass(frozen=True)
class ContractClauseContext:
    """Structural interpretation of one clause containing a blocked marker."""

    actor: ClauseActor
    modality: ClauseModality


def analyze_contract_clause(clause: str) -> ContractClauseContext:
    """Classify who a blocked term concerns and whether it is mandatory."""

    candidate_frame = bool(
        _CANDIDATE_CONTRACT_FRAME_RE.search(clause)
        or _CANDIDATE_OFFER_FRAME_RE.search(clause)
    )
    third_party = (
        _THIRD_PARTY_RELATION_RE.search(clause) is not None
        or _THIRD_PARTY_DUTY_RE.search(clause) is not None
        or (
            _THIRD_PARTY_ENTITY_RE.search(clause) is not None
            and not candidate_frame
        )
    )
    actor = (
        ClauseActor.THIRD_PARTY
        if third_party
        else ClauseActor.CANDIDATE
        if candidate_frame
        else ClauseActor.UNKNOWN
    )

    if (
        (_LEVEL_HEADING_RE.search(clause) or _LEVEL_MENU_FRAME_RE.search(clause))
        and _COMPATIBLE_LEVEL_RE.search(clause)
        and _ALTERNATIVE_CONNECTOR_RE.search(clause)
    ):
        modality = ClauseModality.LEVEL_ALTERNATIVE
    elif _HISTORICAL_RE.search(clause) and not candidate_frame:
        modality = ClauseModality.HISTORICAL
    elif _MANDATORY_RE.search(clause):
        modality = ClauseModality.MANDATORY_OR_UNKNOWN
    elif _PART_TIME_RE.search(clause) and (
        _OPTIONAL_RE.search(clause)
        or (
            _FULL_TIME_RE.search(clause)
            and _ALTERNATIVE_CONNECTOR_RE.search(clause)
        )
    ):
        modality = ClauseModality.OPTIONAL
    else:
        modality = ClauseModality.MANDATORY_OR_UNKNOWN
    return ContractClauseContext(actor=actor, modality=modality)


def find_candidate_contract_signal(
    title: str,
    description: str,
    *,
    blocked_contract_types: tuple[str, ...] = (),
    include_fixed_term: bool = False,
    contractor_is_corroborated: bool = False,
) -> CandidateContractSignal | None:
    """Return the first incompatible marker attached to the advertised role.

    Detection is compositional: marker vocabulary determines the contract
    family, while the surrounding clause determines whether that occurrence
    describes the candidate.  Historical experience, scientific vocabulary,
    optional paths, menus, and third-party responsibilities are excluded before
    a signal can become decisive.
    """

    safe_title, safe_description = sanitize_contract_text(
        norm(title),
        norm(description),
    )
    enabled_families, custom_markers = _enabled_marker_families(
        blocked_contract_types,
        include_fixed_term=include_fixed_term,
    )

    for field, text in (("title", safe_title), ("description", safe_description)):
        clauses = [text] if field == "title" else _CLAUSE_SPLIT_RE.split(text)[::2]
        for clause in clauses:
            for occurrence in _contract_occurrences(
                clause,
                enabled_families=enabled_families,
                custom_markers=custom_markers,
                contractor_is_corroborated=contractor_is_corroborated,
            ):
                start, end, family, marker, raw_marker = occurrence
                suffix = clause[end : min(len(clause), end + 120)]
                if _PROGRAM_RESPONSIBILITY_SUFFIX_RE.match(suffix):
                    continue
                if field == "title" or _is_candidate_contract_occurrence(
                    clause,
                    start=start,
                    end=end,
                    family=family,
                    raw_marker=raw_marker,
                ):
                    return CandidateContractSignal(
                        family=family,
                        marker=marker,
                        raw_marker=raw_marker,
                        field=field,
                    )
    return None


def _is_candidate_contract_occurrence(
    clause: str,
    *,
    start: int,
    end: int,
    family: ContractMarkerFamily,
    raw_marker: str,
) -> bool:
    prefix = clause[max(0, start - 160) : start]
    suffix = clause[end : min(len(clause), end + 120)]
    relation = _has_candidate_offer_relation(prefix, suffix) or _is_short_contract_heading(
        clause,
        start=start,
        end=end,
    )

    if _is_third_party_occurrence(
        prefix=prefix,
        suffix=suffix,
        raw_marker=raw_marker,
        candidate_relation=relation,
    ):
        return False
    if _is_non_decisive_context(
        prefix=prefix,
        suffix=suffix,
    ):
        return False
    if relation:
        return True
    if not prefix.strip() and not suffix.strip():
        return True
    return family is ContractMarkerFamily.FIXED_TERM and "contract" in raw_marker


def _has_candidate_offer_relation(prefix: str, suffix: str) -> bool:
    """Return whether local wording links a marker to the advertised role.

    Weak details such as a duration or ``stage de fin d'etudes`` are useful for
    recognizing a standalone contract label.  They must not override an
    explicit past-experience frame, which is why the decisive relation is
    exposed separately by :func:`_has_explicit_candidate_offer_relation`.
    """

    if _has_explicit_candidate_offer_relation(prefix, suffix):
        return True

    local_suffix = suffix[:90]
    return bool(
        _OFFER_NOUN_SUFFIX_RE.match(local_suffix)
        or _CONTRACT_DETAIL_SUFFIX_RE.match(local_suffix)
        or _DURATION_SUFFIX_RE.match(local_suffix)
        or _FIXED_TERM_ROLE_SUFFIX_RE.match(local_suffix)
        or _SEASONAL_CONTRACT_SUFFIX_RE.match(local_suffix)
        or _EXCLUSIVE_CONTRACT_SUFFIX_RE.match(local_suffix)
    )


def _has_explicit_candidate_offer_relation(prefix: str, suffix: str) -> bool:
    """Return whether syntax explicitly assigns the marker to this candidate."""

    return _explicit_candidate_relation_start(prefix, suffix) is not None


def _explicit_candidate_relation_start(prefix: str, suffix: str) -> int | None:
    """Return where the closest explicit candidate relation starts."""

    predicate_prefix, coordinated = _local_predicate_prefix(prefix)
    local_prefix = predicate_prefix[-140:]
    local_suffix = suffix[:90]
    local_offset = len(prefix) - len(local_prefix)
    relation_starts: list[int] = []

    prefix_patterns = (
        _OFFER_ATTRIBUTE_PREFIX_RE,
        _CURRENT_OFFER_ASSERTION_PREFIX_RE,
        _CONTRACT_LABEL_PREFIX_RE,
        _CURRENT_ROLE_CONTRACT_PREFIX_RE,
        _RECRUITMENT_CONTRACT_PREFIX_RE,
        _CONTRACT_ALTERNATIVE_PREFIX_RE,
        _CANDIDATE_ACTIVITY_PREFIX_RE,
        _CURRENT_OFFER_REFERENCE_PREFIX_RE,
        _OPPORTUNITY_FOR_CANDIDATE_PREFIX_RE,
        _CANDIDATE_OPENING_PREFIX_RE,
        _CANDIDATE_SEARCH_PREFIX_RE,
    )
    for pattern in prefix_patterns:
        if match := pattern.search(local_prefix):
            relation_starts.append(local_offset + match.start())
    if (
        (match := _SHORT_ROLE_CONTRACT_PREFIX_RE.search(local_prefix))
        and not _NON_HEADLINE_STATUS_PREFIX_RE.search(local_prefix)
    ):
        relation_starts.append(local_offset + match.start())
    if (
        (match := _CANDIDATE_CONTRACT_FRAME_RE.search(local_prefix))
        and _CANDIDATE_CONTRACT_FRAME_GAP_RE.fullmatch(
            local_prefix[match.end() :]
        )
    ):
        relation_starts.append(local_offset + match.start())
    if _ATS_CONTRACT_VALUE_PREFIX_RE.fullmatch(local_prefix):
        relation_starts.append(local_offset)
    if _OFFER_STATUS_SUFFIX_RE.match(local_suffix):
        relation_starts.append(len(prefix))
    if (
        (match := _SELF_IDENTIFICATION_PREFIX_RE.search(local_prefix))
        and _SELF_IDENTIFICATION_SUFFIX_RE.match(local_suffix)
    ):
        relation_starts.append(local_offset + match.start())

    action_matches = list(_OFFER_ACTION_RE.finditer(local_prefix))
    if action_matches:
        gap = local_prefix[action_matches[-1].end() :]
        if _OFFER_ACTION_GAP_RE.fullmatch(gap):
            relation_starts.append(local_offset + action_matches[-1].start())
    if coordinated and _COORDINATED_ATTRIBUTE_PREFIX_RE.fullmatch(local_prefix):
        relation_starts.append(local_offset)
    if coordinated:
        action_matches = list(_COORDINATED_OFFER_ACTION_RE.finditer(local_prefix))
        if action_matches:
            gap = local_prefix[action_matches[-1].end() :]
            if _OFFER_ACTION_GAP_RE.fullmatch(gap):
                relation_starts.append(local_offset + action_matches[-1].start())
        if _COORDINATED_CANDIDATE_ACTIVITY_RE.fullmatch(local_prefix):
            relation_starts.append(local_offset)
    return max(relation_starts, default=None)


def _local_predicate_prefix(prefix: str) -> tuple[str, bool]:
    """Return text after the latest conjunction that starts a new predicate."""

    boundaries = list(_PREDICATE_BOUNDARY_RE.finditer(prefix))
    if not boundaries:
        return prefix, False
    return prefix[boundaries[-1].end() :], True


def _is_short_contract_heading(clause: str, *, start: int, end: int) -> bool:
    """Recognize a compact contract heading with a duration or location."""

    if len(clause.strip()) > 80:
        return False
    prefix = clause[:start]
    suffix = clause[end:]
    if not _SHORT_HEADING_PREFIX_RE.fullmatch(prefix):
        return False
    if not suffix.strip():
        return not prefix.strip(" \t:=-")
    return _SHORT_HEADING_SUFFIX_RE.fullmatch(suffix) is not None


def _is_third_party_occurrence(
    *,
    prefix: str,
    suffix: str,
    raw_marker: str,
    candidate_relation: bool,
) -> bool:
    local_prefix, _ = _local_predicate_prefix(prefix)
    organization_identity = bool(
        re.fullmatch(r"(?:independant(?:e)?|independent)", raw_marker)
        and (
            _ORGANIZATION_INDEPENDENCE_PREFIX_RE.search(local_prefix[-120:])
            or _ORGANIZATION_INDEPENDENCE_SUFFIX_RE.match(suffix[:100])
        )
    )
    if organization_identity:
        return True
    if _PROGRAM_RESPONSIBILITY_SUFFIX_RE.match(suffix):
        return True
    if _THIRD_PARTY_MANAGER_SUFFIX_RE.match(suffix):
        return True
    duty_object = bool(
        _THIRD_PARTY_ROLE_TERM_RE.fullmatch(raw_marker)
        and _THIRD_PARTY_DUTY_VERB_RE.search(local_prefix[-120:])
    )
    if duty_object:
        return True

    possessive_program = bool(
        re.search(r"\b(?:our|their|notre|leur)\s*$", prefix)
        and re.match(r"^\s*program(?:me)?\b", suffix)
    )
    if possessive_program:
        return True

    third_party_subject = bool(
        _THIRD_PARTY_ENTITY_RE.search(local_prefix[-90:])
        or (
            re.search(r"\b(?:contracts?|contrats?)\b", local_prefix[-60:])
            and _THIRD_PARTY_ENTITY_RE.search(suffix[:90])
        )
    )
    collaborator_object = bool(
        _THIRD_PARTY_ROLE_TERM_RE.fullmatch(raw_marker)
        and re.search(
            r"\b(?:with|alongside|avec)\s+(?:(?:an?|un|une|des?)\s+)?$",
            local_prefix,
        )
    )
    return collaborator_object or (third_party_subject and not candidate_relation)


def _is_non_decisive_context(
    *,
    prefix: str,
    suffix: str,
) -> bool:
    local_prefix, _ = _local_predicate_prefix(prefix)
    local_context = f"{local_prefix[-160:]} {suffix[:120]}"
    if (
        (
            _LEVEL_HEADING_RE.search(local_context)
            or _LEVEL_MENU_FRAME_RE.search(local_context)
        )
        and _COMPATIBLE_LEVEL_RE.search(local_context)
        and _ALTERNATIVE_CONNECTOR_RE.search(local_context)
    ):
        return True
    if (
        _GENERIC_LISTING_RE.search(local_context)
        or _JOB_SEARCH_BOILERPLATE_RE.search(local_context)
        or _RECRUITMENT_SERVICE_CONTEXT_RE.search(local_context)
    ):
        return True
    if (
        _NEGATED_MARKER_PREFIX_RE.search(local_prefix[-100:])
        or _NEGATED_MARKER_SUFFIX_RE.match(suffix[:100])
    ):
        return True
    optional_prefix = _OPTIONAL_MARKER_CONTEXT_RE.search(local_prefix[-80:])
    local_suffix = suffix[:80]
    optional_suffix = _OPTIONAL_MARKER_CONTEXT_RE.search(local_suffix)
    optional_targets_other_contract = bool(
        optional_suffix
        and _PERMANENT_TERM_RE.search(local_suffix[: optional_suffix.start()])
    )
    if optional_prefix or (optional_suffix and not optional_targets_other_contract):
        return not _MANDATORY_RE.search(local_context)
    if _PAST_CANDIDATE_ACTION_RE.search(local_prefix):
        return True
    if _HISTORICAL_MARKER_SUFFIX_RE.match(suffix[:70]):
        return True

    historical_window = prefix[-160:]
    historical_matches = list(_HISTORICAL_TOKEN_RE.finditer(historical_window))
    if not historical_matches:
        return False
    latest_historical_start = (
        len(prefix) - len(historical_window) + historical_matches[-1].start()
    )
    explicit_relation_start = _explicit_candidate_relation_start(prefix, suffix)
    return (
        explicit_relation_start is None
        or explicit_relation_start <= latest_historical_start
    )


def sanitize_contract_text(title: str, description: str) -> tuple[str, str]:
    """Return visible text containing only candidate-owned decisive markers."""

    safe_title = _mask_patterns(title, _NON_CONTRACT_STAGE_TITLE_PATTERNS)
    safe_title = _mask_patterns(safe_title, _NON_CONTRACT_SCIENTIFIC_PATTERNS)

    safe_description = _mask_patterns(description, _NON_CONTRACT_STAGE_TITLE_PATTERNS)
    safe_description = _mask_patterns(
        safe_description,
        _NON_CONTRACT_SCIENTIFIC_PATTERNS,
    )
    safe_description = _mask_patterns(
        safe_description,
        _NON_CONTRACT_HISTORICAL_PATTERNS,
    )
    safe_description = _mask_patterns(
        safe_description,
        _NON_CONTRACT_ROTATION_PATTERNS,
    )
    parts = _CLAUSE_SPLIT_RE.split(safe_description)
    for index in range(0, len(parts), 2):
        clause = parts[index]
        if not _BLOCKED_TERM_RE.search(clause):
            continue
        clause = _mask_third_party_markers(clause)
        clause = _mask_non_decisive_markers(clause)
        parts[index] = clause
    return safe_title, "".join(parts)


def sanitize_candidate_permanent_text(title: str, description: str) -> tuple[str, str]:
    """Remove permanent-contract words that clearly belong to third parties."""

    parts = _CLAUSE_SPLIT_RE.split(description)
    for index in range(0, len(parts), 2):
        clause = parts[index]
        if _PERMANENT_TERM_RE.search(clause):
            parts[index] = _mask_third_party_permanent_markers(clause)
    return title, "".join(parts)


def has_candidate_mandatory_part_time(text: str) -> bool:
    """Return true for an explicit candidate-owned mandatory part-time clause."""

    for clause in _CLAUSE_SPLIT_RE.split(text)[::2]:
        if not _PART_TIME_RE.search(clause):
            continue
        context = analyze_contract_clause(clause)
        if (
            context.actor is not ClauseActor.THIRD_PARTY
            and context.modality is ClauseModality.MANDATORY_OR_UNKNOWN
            and (
                _CANDIDATE_CONTRACT_FRAME_RE.search(clause)
                or _MANDATORY_RE.search(clause)
                or re.search(
                    r"\b(?:part[- ]time|temps\s+partiel|mi[- ]temps)\b"
                    r".{0,35}\b(?:role|position|job|applications?|schedule|poste|"
                    r"emploi|candidatures?|horaire)\b",
                    clause,
                )
            )
        ):
            return True
    return False


def _mask_patterns(text: str, patterns: tuple[str, ...]) -> str:
    masked = text
    for pattern in patterns:
        masked = re.sub(pattern, " ", masked)
    return masked


def _mask_third_party_markers(clause: str) -> str:
    """Mask only occurrences locally owned by a colleague or managed team."""

    def replace(match: re.Match[str]) -> str:
        prefix = clause[max(0, match.start() - 120) : match.start()]
        suffix = clause[match.end() : min(len(clause), match.end() + 120)]
        marker = match.group(0)

        candidate_relation = _has_candidate_offer_relation(prefix, suffix)
        return (
            " "
            if _is_third_party_occurrence(
                prefix=prefix,
                suffix=suffix,
                raw_marker=marker,
                candidate_relation=candidate_relation,
            )
            else marker
        )

    return _BLOCKED_TERM_RE.sub(replace, clause)


def _mask_non_decisive_markers(clause: str) -> str:
    """Mask historical, optional, and level-menu occurrences independently."""

    def replace(match: re.Match[str]) -> str:
        prefix = clause[max(0, match.start() - 160) : match.start()]
        suffix = clause[match.end() : min(len(clause), match.end() + 120)]
        return (
            " "
            if _is_non_decisive_context(
                prefix=prefix,
                suffix=suffix,
            )
            else match.group(0)
        )

    return _BLOCKED_TERM_RE.sub(replace, clause)


def _mask_third_party_permanent_markers(clause: str) -> str:
    """Mask compatible-contract words owned by staff, teams, or consultants."""

    def replace(match: re.Match[str]) -> str:
        prefix = clause[max(0, match.start() - 160) : match.start()]
        suffix = clause[match.end() : min(len(clause), match.end() + 120)]
        local_prefix, _ = _local_predicate_prefix(prefix)
        candidate_relation = _has_candidate_offer_relation(prefix, suffix)
        third_party_relation = bool(
            _THIRD_PARTY_ENTITY_RE.search(local_prefix[-80:])
            or re.match(
                r"\s*(?:employees?|staff|personnel|collaborateurs?|consultants?|team|equipe)\b",
                suffix,
            )
        )
        return " " if third_party_relation and not candidate_relation else match.group(0)

    return _PERMANENT_TERM_RE.sub(replace, clause)


__all__ = [
    "CandidateContractSignal",
    "ClauseActor",
    "ClauseModality",
    "ContractMarkerFamily",
    "ContractClauseContext",
    "analyze_contract_clause",
    "find_candidate_contract_signal",
    "has_candidate_mandatory_part_time",
    "sanitize_candidate_permanent_text",
    "sanitize_contract_text",
]
