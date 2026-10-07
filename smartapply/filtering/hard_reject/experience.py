"""Adapt the neutral experience assessment to a hard-rejection decision."""

from __future__ import annotations

from smartapply.filtering.experience_requirements import (
    ExperienceAssessment,
    ExperienceRequirement,
    assess_candidate_experience,
)
from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.types import Evidence, GateDecision, GateStatus

_GATE = "experience"


def evaluate_experience(context: HardRejectContext) -> GateDecision:
    """Reject only a resolved mandatory requirement at or above the ceiling."""

    assessment = _assessment(context)
    if assessment.conflict:
        evidence = tuple(
            _evidence(item)
            for item in (
                ([assessment.structured] if assessment.structured else [])
                + list(assessment.visible)
            )
        )
        notes = tuple(
            item
            for item in (assessment.structured_note, "experience_evidence_conflict")
            if item
        )
        return GateDecision.unresolved(
            _GATE,
            conflicts=(assessment.conflict,),
            evidence=evidence,
            notes=notes,
        )

    threshold_months = context.rules.max_required_years * 12
    blockers = [
        requirement
        for requirement in assessment.mandatory
        if requirement.months >= threshold_months
    ]
    if not blockers:
        return GateDecision.clear(
            _GATE,
            notes=(assessment.structured_note,) if assessment.structured_note else (),
        )

    # Separate mandatory requirements are cumulative. A lower requirement for
    # one sub-skill must not erase a five-year global requirement. A numeric
    # range is already represented by its lower bound during neutral parsing.
    blocker = min(blockers, key=lambda item: item.months)
    years = _format_number(blocker.years)
    return GateDecision(
        gate=_GATE,
        status=GateStatus.BLOCKED,
        reason_codes=(f"experience_required_too_high:{years}+ years",),
        evidence=(_evidence(blocker),),
        notes=(assessment.structured_note,) if assessment.structured_note else (),
    )


def _assessment(context: HardRejectContext) -> ExperienceAssessment:
    return assess_candidate_experience(
        title=context.title,
        description=context.description,
        facts=context.facts,
        max_required_years=context.rules.max_required_years,
    )


def _evidence(requirement: ExperienceRequirement) -> Evidence:
    return Evidence(
        rule_id="candidate_experience_minimum",
        source=requirement.source,
        quote=requirement.quote,
        field=requirement.field,
    )


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
