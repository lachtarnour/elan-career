"""Hard-rejection adapter for neutral seniority signals."""

from __future__ import annotations

import re

from smartapply.filtering.experience_requirements import assess_candidate_experience
from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.types import Evidence, GateDecision, GateStatus
from smartapply.filtering.seniority import (
    SeniorityAssessment,
    SeniorityAssessmentStatus,
    SenioritySignalKind,
    assess_seniority,
)

_GATE = "seniority"


def evaluate_seniority(context: HardRejectContext) -> GateDecision:
    """Translate the shared seniority assessment into a gate decision."""

    assessment = assess_seniority(
        context.title,
        context.description,
        context.rules.seniority_title_hard_reject,
    )
    if assessment.status is SeniorityAssessmentStatus.BLOCKED:
        body = assess_seniority("", context.description)
        if body.kind is SenioritySignalKind.HIERARCHICAL_MANAGEMENT:
            return _blocked_decision(body)
        hierarchical_title = re.search(
            r"\b(?:team\s+lead(?:er)?|lead\s+of\s+(?:a|the)\s+team)\b",
            context.title,
        ) is not None
        grade_only = (
            assessment.kind is SenioritySignalKind.EXPLICIT_TITLE
            and assessment.title_marker in {"sr", "lead", "leader"}
            and not hierarchical_title
        )
        experience = assess_candidate_experience(
            title=context.title,
            description=context.description,
            facts=context.facts,
            max_required_years=context.rules.max_required_years,
        )
        low_floor = any(
            item.required and item.years < context.rules.max_required_years
            for item in experience.visible
        ) and not any(
            item.required and item.years >= context.rules.max_required_years
            for item in experience.visible
        )
        grade_conflict = low_floor and not hierarchical_title and (
            assessment.kind is SenioritySignalKind.HIDDEN_ROLE
            or assessment.title_marker in {"senior", "sr", "lead", "leader"}
        )
        if grade_only or grade_conflict:
            return GateDecision.unresolved(
                _GATE,
                conflicts=(
                    "seniority_label_conflicts_with_eligible_minimum"
                    if grade_conflict
                    else "seniority_label_without_firm_requirement",
                ),
                evidence=(
                    Evidence(
                        rule_id="ambiguous_seniority_label",
                        source="visible_text",
                        quote=_required_quote(assessment),
                        field="title" if assessment.title_marker else "description",
                    ),
                ),
            )
        return _blocked_decision(assessment)
    if assessment.status is SeniorityAssessmentStatus.UNRESOLVED:
        return _unresolved_decision(assessment)
    return GateDecision.clear(_GATE)


def _blocked_decision(assessment: SeniorityAssessment) -> GateDecision:
    if assessment.kind is SenioritySignalKind.EXPLICIT_TITLE:
        return GateDecision(
            gate=_GATE,
            status=GateStatus.BLOCKED,
            reason_codes=(f"seniority_in_title:{assessment.title_marker}",),
            evidence=(
                Evidence(
                    rule_id=SenioritySignalKind.EXPLICIT_TITLE.value,
                    source="visible_title",
                    quote=_required_quote(assessment),
                    field="title",
                ),
            ),
        )

    return GateDecision(
        gate=_GATE,
        status=GateStatus.BLOCKED,
        reason_codes=("seniority_or_leadership_in_description",),
        evidence=(
            Evidence(
                rule_id=_required_kind(assessment).value,
                source="visible_description",
                quote=_required_quote(assessment),
                field="description",
            ),
        ),
    )


def _unresolved_decision(assessment: SeniorityAssessment) -> GateDecision:
    if not assessment.conflict:
        raise ValueError("Unresolved seniority assessment is missing its conflict")
    return GateDecision.unresolved(
        _GATE,
        conflicts=(assessment.conflict,),
        evidence=(
            Evidence(
                rule_id=SenioritySignalKind.AMBIGUOUS_TITLE.value,
                source="visible_title",
                quote=_required_quote(assessment),
                field="title",
            ),
        ),
    )


def _required_kind(assessment: SeniorityAssessment) -> SenioritySignalKind:
    if assessment.kind is None:
        raise ValueError("Seniority assessment is missing its kind")
    return assessment.kind


def _required_quote(assessment: SeniorityAssessment) -> str:
    if not assessment.quote:
        raise ValueError("Seniority assessment is missing its quote")
    return assessment.quote
