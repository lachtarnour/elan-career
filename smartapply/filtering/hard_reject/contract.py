"""Deterministic contract-compatibility gate.

This module answers one narrow question: does the offer contain reliable proof
of an incompatible contract or work schedule?  It deliberately does not rank
the role and does not classify relevance.
"""

from __future__ import annotations

import re

from smartapply.filtering.contract_signals import (
    has_freelance_contract_context,
    has_part_time_contract_context,
    has_research_degree_title,
)
from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.contract_context import (
    ContractMarkerFamily,
    find_candidate_contract_signal,
    has_candidate_mandatory_part_time,
    sanitize_candidate_permanent_text,
    sanitize_contract_text,
)
from smartapply.filtering.hard_reject.contract_evidence import (
    full_time_evidence,
    marker_needles,
    permanent_contract_evidence,
    reason_value,
    research_cdd_label,
    structured_source,
    visible_evidence,
)
from smartapply.filtering.hard_reject.contract_pay import structured_daily_pay_path
from smartapply.filtering.hard_reject.contract_resolution import (
    ContractBlocker,
    decision_from_evidence,
)
from smartapply.filtering.hard_reject.types import Evidence, GateDecision
from smartapply.filtering.text import norm
from smartapply.utils.contracts import (
    CDD_EQUIVALENT_TAGS,
    TAG_CONTRACTOR,
    TAG_PART_TIME,
    blocked_contract_tags_from_tags,
    contract_type_tags,
    contract_types_to_tags,
)

_PRESTATAIRE_MARKERS = (
    r"\b(?:statut|mission|poste|role)\s+(?:de\s+|en\s+)?prestataire\b",
    r"\bprestataire\s+(?:independant|freelance)\b",
    r"\b(?:tjm|tarif\s+journalier)\b",
    r"\b\d{2,4}\s*(?:€|euros?|eur)\s*(?:/|par\s+)(?:jour|day|daily)\b",
)

_GENERIC_CONTRACT_ROLE_RE = re.compile(r"\bcontract\s+(?:role|position|job)\b")
_MIXED_INTERNSHIP_LEVEL_RE = re.compile(
    r"\b(?:stage\s+de\s+fin\s+d[' ]?etudes|internship)\b"
    r"[^.!?;]{0,100}(?:,|/|\bou\b|\bor\b)"
    r"[^.!?;]{0,100}\b(?:junior|intermediaire|intermediate|entry[- ]level|mid)\b|"
    r"\b(?:junior|intermediaire|intermediate|entry[- ]level|mid)\b"
    r"[^.!?;]{0,100}(?:,|/|\bou\b|\bor\b)"
    r"[^.!?;]{0,100}\b(?:stage\s+de\s+fin\s+d[' ]?etudes|internship)\b"
)
_IMPLAUSIBLE_WORK_TIME_WARNING_PREFIX = (
    "structured_work_time_implausible_weekly_hours:"
)

def evaluate_contract(context: HardRejectContext) -> GateDecision:
    """Evaluate only deterministic contract and work-time blockers.

    Contradictory contract evidence remains ``UNRESOLVED`` so a later stage can
    route the offer without weakening the other hard-rejection gates.  A hard
    rejection requires an explicit incompatible arrangement with no compatible
    alternative in metadata or visible text.
    """

    safe_title, safe_description = _visible_text_without_false_context(context)
    permanent_title, permanent_description = sanitize_candidate_permanent_text(
        safe_title,
        safe_description,
    )
    permanent_evidence = permanent_contract_evidence(
        context,
        safe_title=permanent_title,
        safe_description=permanent_description,
    )
    compatible_full_time_evidence = full_time_evidence(
        context,
        safe_title=safe_title,
        safe_description=safe_description,
    )
    source_uncertainty_evidence = _structured_work_time_uncertainty_evidence(
        context
    )
    notes: list[str] = []
    if source_uncertainty_evidence:
        notes.append("structured_work_time_untrusted:implausible_weekly_hours")
    blockers: list[ContractBlocker] = []

    raw_tags = contract_type_tags(context.job.contract_type)
    structured_tags = contract_type_tags(context.facts.structured_contract_type)
    structured_pay_path = structured_daily_pay_path(
        getattr(context.job, "source_data", None)
    )
    contractor_is_corroborated = _contractor_is_corroborated(
        context,
        safe_title=safe_title,
        safe_description=safe_description,
        structured_pay_path=structured_pay_path,
    )
    fixed_term_signal = find_candidate_contract_signal(
        safe_title,
        safe_description,
        include_fixed_term=True,
    )
    fixed_term_in_title = bool(
        fixed_term_signal
        and fixed_term_signal.family is ContractMarkerFamily.FIXED_TERM
        and fixed_term_signal.field == "title"
    )
    fixed_term_in_description = bool(
        fixed_term_signal
        and fixed_term_signal.family is ContractMarkerFamily.FIXED_TERM
        and fixed_term_signal.field == "description"
    )
    if not contractor_is_corroborated:
        safe_title = _GENERIC_CONTRACT_ROLE_RE.sub(" ", safe_title)
        safe_description = _GENERIC_CONTRACT_ROLE_RE.sub(" ", safe_description)
    elif structured_pay_path and _has_ambiguous_contractor_signal(
        context,
        safe_title=safe_title,
        safe_description=safe_description,
    ):
        notes.append(f"contractor_corroboration:structured_pay:{structured_pay_path}")

    if norm(context.job.contract_type).strip() == "prestataire" and contractor_is_corroborated:
        raw_tags.add(TAG_CONTRACTOR)
    if (
        norm(context.facts.structured_contract_type).strip() == "prestataire"
        and contractor_is_corroborated
    ):
        structured_tags.add(TAG_CONTRACTOR)

    raw_tags = _drop_uncorroborated_contractor_tag(
        value=context.job.contract_type,
        tags=raw_tags,
        corroborated=contractor_is_corroborated,
        note_prefix="contract_type",
        notes=notes,
    )
    structured_tags = _drop_uncorroborated_contractor_tag(
        value=context.facts.structured_contract_type,
        tags=structured_tags,
        corroborated=contractor_is_corroborated,
        note_prefix="structured_contract",
        notes=notes,
    )

    research_cdd_exception = has_research_degree_title(context.title) and bool(
        (raw_tags | structured_tags) & CDD_EQUIVALENT_TAGS
        or fixed_term_in_title
        or fixed_term_in_description
    )
    if research_cdd_exception:
        notes.append("contract_exception:research_degree_title_cdd")
        notes.append(
            f"contract_ok:{research_cdd_label(context)}:research_degree_title"
        )

    if context.facts.structured_alternance is True:
        blockers.append(
            ContractBlocker(
                reason_code="blocked_contract_structured:alternance (tag 'apprenticeship')",
                evidence=Evidence(
                    rule_id="contract.structured_alternance",
                    source=structured_source(context, "structured_alternance"),
                    quote="structured_alternance=true",
                    field="structured_alternance",
                ),
            )
        )

    raw_incompatible = _without_research_cdd(
        blocked_contract_tags_from_tags(
            raw_tags,
            context.rules.accepted_contract_types,
        ),
        research_cdd_exception,
    )
    if context.contract and raw_incompatible:
        for tag in sorted(raw_incompatible):
            blockers.append(
                ContractBlocker(
                    reason_code=f"blocked_contract_type:{context.contract} (tag '{tag}')",
                    evidence=Evidence(
                        rule_id="contract.metadata_incompatible",
                        source="job.contract_type",
                        quote=str(context.job.contract_type or context.contract),
                        field="contract_type",
                    ),
                    conflicts_with_permanent_contract=tag != TAG_PART_TIME,
                    conflicts_with_full_time=tag == TAG_PART_TIME,
                )
            )

    structured_value = context.facts.structured_contract_type
    structured_normalized = norm(structured_value)
    structured_incompatible = _without_research_cdd(
        blocked_contract_tags_from_tags(
            structured_tags,
            context.rules.accepted_contract_types,
        ),
        research_cdd_exception,
    )
    if structured_normalized and structured_incompatible:
        for tag in sorted(structured_incompatible):
            blockers.append(
                ContractBlocker(
                    reason_code=(
                        f"blocked_contract_structured:{structured_normalized} "
                        f"(tag '{tag}')"
                    ),
                    evidence=Evidence(
                        rule_id="contract.source_fact_incompatible",
                        source=(
                            context.facts.structured_contract_source
                            or structured_source(context, "structured_contract_type")
                        ),
                        quote=str(structured_value),
                        field="structured_contract_type",
                    ),
                    conflicts_with_permanent_contract=tag != TAG_PART_TIME,
                    conflicts_with_full_time=tag == TAG_PART_TIME,
                )
            )

    work_time = context.facts.structured_work_time
    part_time_is_accepted = TAG_PART_TIME in contract_types_to_tags(
        context.rules.accepted_contract_types
    )
    if (
        not part_time_is_accepted
        and TAG_PART_TIME in contract_type_tags(work_time)
    ):
        blockers.append(
            ContractBlocker(
                reason_code=(
                    "blocked_work_time_structured:"
                    f"{reason_value(work_time)} (tag 'part_time')"
                ),
                evidence=Evidence(
                    rule_id="contract.structured_part_time",
                    source=(
                        context.facts.structured_work_time_source
                        or structured_source(context, "structured_work_time")
                    ),
                    quote=str(work_time),
                    field="structured_work_time",
                ),
                conflicts_with_permanent_contract=False,
                conflicts_with_full_time=True,
                conflicts_with_source_uncertainty=bool(
                    source_uncertainty_evidence
                ),
            )
        )

    visible_part_time = has_part_time_contract_context(
        safe_title,
        safe_description,
    ) or has_candidate_mandatory_part_time(safe_description)
    if not part_time_is_accepted and visible_part_time:
        source, quote, field = visible_evidence(
            context,
            title_matches=has_part_time_contract_context(safe_title, ""),
            needles=("temps partiel", "mi-temps", "mi temps", "part-time", "part time"),
        )
        blockers.append(
            ContractBlocker(
                reason_code="blocked_work_time_visible_text:part_time",
                evidence=Evidence(
                    rule_id="contract.visible_part_time",
                    source=source,
                    quote=quote,
                    field=field,
                ),
                conflicts_with_permanent_contract=False,
                conflicts_with_full_time=True,
            )
        )

    blocked_markers = _blocked_markers_without_research_cdd(
        context,
        research_cdd_exception=research_cdd_exception,
    )
    visible_signal = find_candidate_contract_signal(
        safe_title,
        safe_description,
        blocked_contract_types=blocked_markers,
        contractor_is_corroborated=contractor_is_corroborated,
    )
    mixed_level_menu = _has_mixed_internship_level_menu(context.description)
    if visible_signal:
        marker = visible_signal.marker
        is_mixed_internship_level = (
            visible_signal.family is ContractMarkerFamily.INTERNSHIP
            and mixed_level_menu
        )
        if not is_mixed_internship_level or compatible_full_time_evidence:
            source, quote, field = visible_evidence(
                context,
                title_matches=visible_signal.field == "title",
                needles=marker_needles(marker),
            )
            blockers.append(
                ContractBlocker(
                    reason_code=f"blocked_contract_visible_text:{marker}",
                    evidence=Evidence(
                        rule_id="contract.visible_incompatible",
                        source=source,
                        quote=quote,
                        field=field,
                    ),
                    conflicts_with_full_time=is_mixed_internship_level,
                    overrides_metadata_permanent=True,
                )
            )
    elif mixed_level_menu and compatible_full_time_evidence:
        source, quote, field = visible_evidence(
            context,
            title_matches=False,
            needles=("stage", "internship"),
        )
        blockers.append(
            ContractBlocker(
                reason_code="blocked_contract_visible_text:stage",
                evidence=Evidence(
                    rule_id="contract.visible_incompatible_level_alternative",
                    source=source,
                    quote=quote,
                    field=field,
                ),
                conflicts_with_full_time=True,
            )
        )

    accepted_tags = contract_types_to_tags(context.rules.accepted_contract_types)
    if (
        not accepted_tags & CDD_EQUIVALENT_TAGS
        and not research_cdd_exception
        and (fixed_term_in_title or fixed_term_in_description)
    ):
        title_matches = fixed_term_in_title
        source, quote, field = visible_evidence(
            context,
            title_matches=title_matches,
            needles=(
                "cdd",
                "interim",
                "intérim",
                "fixed-term",
                "fixed term",
                "temporary",
                "temporaire",
                "contract",
            ),
        )
        blockers.append(
            ContractBlocker(
                reason_code="blocked_contract_visible_text:cdd",
                evidence=Evidence(
                    rule_id="contract.visible_fixed_term",
                    source=source,
                    quote=quote,
                    field=field,
                ),
                overrides_metadata_permanent=True,
            )
        )

    # Preserve configured markers that do not map to a canonical tag.  Known
    # markers were already handled above and are omitted here to avoid duplicate
    # reasons for the same metadata value.
    if context.contract and not raw_incompatible:
        configured_marker = _configured_raw_marker(
            context.contract,
            blocked_markers,
        )
        if configured_marker:
            blockers.append(
                ContractBlocker(
                    reason_code=(
                        f"blocked_contract_type:{context.contract} "
                        f"(matched '{configured_marker}')"
                    ),
                    evidence=Evidence(
                        rule_id="contract.configured_metadata_marker",
                        source="job.contract_type",
                        quote=str(context.job.contract_type or context.contract),
                        field="contract_type",
                    ),
                )
            )

    return decision_from_evidence(
        blockers=blockers,
        permanent_evidence=permanent_evidence,
        full_time_evidence=compatible_full_time_evidence,
        source_uncertainty_evidence=source_uncertainty_evidence,
        notes=notes,
    )


def _visible_text_without_false_context(context: HardRejectContext) -> tuple[str, str]:
    return sanitize_contract_text(context.title, context.description)


def _has_mixed_internship_level_menu(value: str | None) -> bool:
    flattened = re.sub(r"[\s·•]+", " ", norm(value)).strip()
    return bool(_MIXED_INTERNSHIP_LEVEL_RE.search(flattened))


def _structured_work_time_uncertainty_evidence(
    context: HardRejectContext,
) -> tuple[Evidence, ...]:
    if not any(
        warning.startswith(_IMPLAUSIBLE_WORK_TIME_WARNING_PREFIX)
        for warning in context.facts.warnings
    ):
        return ()

    source_data = getattr(context.job, "source_data", None)
    raw_label = None
    if isinstance(source_data, dict):
        raw_label = source_data.get("dureeTravailLibelle")
    quote = str(raw_label or context.facts.structured_work_time or "unknown")
    source = (
        "francetravail:dureeTravailLibelle"
        if raw_label
        else context.facts.structured_work_time_source
        or structured_source(context, "structured_work_time")
    )
    return (
        Evidence(
            rule_id="contract.source_fact_implausible_work_time",
            source=source,
            quote=quote,
            field=(
                "source_data.dureeTravailLibelle"
                if raw_label
                else "structured_work_time"
            ),
        ),
    )


def _contractor_is_corroborated(
    context: HardRejectContext,
    *,
    safe_title: str,
    safe_description: str,
    structured_pay_path: str | None,
) -> bool:
    if has_freelance_contract_context(safe_title, safe_description):
        # ``contract role`` alone is deliberately ambiguous.  Explicit
        # contractor/freelance wording remains decisive.
        without_generic = _GENERIC_CONTRACT_ROLE_RE.sub(" ", f"{safe_title}\n{safe_description}")
        if re.search(r"\b(?:contractor|freelance)\b", without_generic):
            return True
    visible_text = f"{safe_title}\n{safe_description}"
    if any(re.search(pattern, visible_text) for pattern in _PRESTATAIRE_MARKERS):
        return True
    return bool(structured_pay_path)


def _has_ambiguous_contractor_signal(
    context: HardRejectContext,
    *,
    safe_title: str,
    safe_description: str,
) -> bool:
    labels = {
        norm(context.job.contract_type).strip(),
        norm(context.facts.structured_contract_type).strip(),
    }
    return bool(labels & {"contract", "prestataire"}) or bool(
        _GENERIC_CONTRACT_ROLE_RE.search(f"{safe_title}\n{safe_description}")
    )


def _drop_uncorroborated_contractor_tag(
    *,
    value: str | None,
    tags: set[str],
    corroborated: bool,
    note_prefix: str,
    notes: list[str],
) -> set[str]:
    kept_tags = set(tags)
    normalized = norm(value).strip()
    if (
        normalized in {"contract", "prestataire"}
        and TAG_CONTRACTOR in kept_tags
        and not corroborated
    ):
        kept_tags.remove(TAG_CONTRACTOR)
        notes.append(f"{note_prefix}_uncorroborated:{normalized}")
    elif normalized == "prestataire" and not corroborated:
        # Some normalizers leave this label without a canonical contractor tag.
        notes.append(f"{note_prefix}_uncorroborated:prestataire")
    return kept_tags


def _without_research_cdd(tags: set[str], exception: bool) -> set[str]:
    if not exception:
        return tags
    return tags - CDD_EQUIVALENT_TAGS


def _blocked_markers_without_research_cdd(
    context: HardRejectContext,
    *,
    research_cdd_exception: bool,
) -> tuple[str, ...]:
    if not research_cdd_exception:
        return context.rules.blocked_contract_types
    return tuple(
        marker
        for marker in context.rules.blocked_contract_types
        if not contract_type_tags(marker) & CDD_EQUIVALENT_TAGS
    )


def _configured_raw_marker(contract: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        normalized = norm(marker)
        if normalized and normalized in contract:
            return normalized
    return None
