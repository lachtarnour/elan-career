"""Calculate a score from model levels and retain advisory evidence diagnostics."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from smartapply.llm.fit_context import (
    POLICY,
    POLICY_VERSION,
    fingerprint,
    offer_references,
    resolve,
)
from smartapply.llm.fit_validation import AXES, POSITIVE, inspect_evaluation
from smartapply.llm.schemas import JobFitAssessment, JobFitEvaluation, OfferAnalysis


def calculate_fit_score(
    missions: int, essentials: int, responsibilities: int, issue_codes: list[str]
) -> tuple[float, dict]:
    levels = (missions, essentials, responsibilities)
    if any(type(level) is not int or not 0 <= level <= 4 for level in levels):
        raise ValueError("Invalid dimension level")
    codes = set(issue_codes)
    if codes - set(POLICY["caps"]) - {"limited_question", "offer_clarity"}:
        raise ValueError("Unknown issue code")
    raw = (
        sum(
            Decimal(weight * level) for weight, level in zip(POLICY["weights"], levels, strict=True)
        )
        / 400
    )
    caps = {code: Decimal(POLICY["caps"][code]) for code in sorted(codes) if code in POLICY["caps"]}
    if missions <= 1:
        caps["weak_mission_support"] = Decimal(POLICY["guards"]["missions_le_1"])
    if min(levels) <= 2:
        caps["partial_dimension_support"] = Decimal(POLICY["guards"]["any_dimension_le_2"])
    capped = min([raw, *caps.values()])
    score = capped.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(score), {
        "policy_version": POLICY_VERSION,
        "calculation_version": POLICY["calculation_version"],
        "raw_score": str(raw),
        "applied_caps": {k: str(v) for k, v in caps.items()},
        "limiting_caps": [k for k, v in caps.items() if v == capped and v < raw],
        "final_score": str(score),
    }


def evaluate_fit(
    evaluation: JobFitEvaluation,
    context: dict,
    offer: OfferAnalysis,
    *,
    extraction_provenance: dict | None = None,
) -> tuple[JobFitAssessment, dict]:
    if context["policy"] != POLICY:
        raise ValueError("Scoring policy has changed; this result requires explicit resumption")
    # Schema validation is limited to usable data (in particular the three levels).
    # Evidence inconsistencies are recorded; they cannot veto this score.
    evaluation = JobFitEvaluation.model_validate(evaluation.model_dump())
    warnings = inspect_evaluation(evaluation, context, offer)
    score, calculation = calculate_fit_score(
        *(getattr(evaluation.dimensions, axis).level for axis in AXES),
        [issue.code for issue in evaluation.issues],
    )
    reasons = []
    for axis in AXES:
        for finding in getattr(evaluation.dimensions, axis).findings:
            if finding.support in POSITIVE:
                text = finding.text.strip()
                prefix = {"declared_only": "Déclaré : ", "transferable": "Transférable : "}.get(
                    finding.support, ""
                )
                refs = [
                    context["candidate_refs"][ref]
                    for ref in finding.candidate_refs
                    if ref in context["candidate_refs"]
                ]
                if any(
                    ref["source_type"] == "bullet"
                    and resolve(context["candidate_facts"], ref)["evidence_level"]
                    == "self_reported"
                    for ref in refs
                ):
                    prefix = "Autodéclaré : " + prefix
                if text and prefix + text not in reasons:
                    reasons.append(prefix + text)
    risks = list(offer.offer_risks)
    for issue in evaluation.issues:
        prefix = {
            "offer_information": "Offre à clarifier : ",
            "preference_alignment": "Orientation/préférence : ",
            "candidate_evidence": "Appui candidat à préciser : ",
        }[issue.origin]
        if issue.origin == "candidate_evidence" and issue.axis in AXES:
            related = [
                finding.support
                for finding in getattr(evaluation.dimensions, issue.axis).findings
                if set(finding.offer_refs) & set(issue.offer_refs)
            ]
            if "contradicted" in related:
                prefix = "Écart établi : "
            elif "not_documented" in related:
                prefix = "Non documenté : "
        text = prefix + issue.text.strip()
        if text not in risks:
            risks.append(text)
    public = JobFitAssessment(
        fit_score=score, match_reasons=reasons[:5], risks=list(dict.fromkeys(risks))
    )
    diagnostics = {
        **calculation,
        "evaluation": evaluation.model_dump(mode="json"),
        "diagnostic_checks": {
            "mode": "advisory",
            "status": "warnings" if warnings else "ok",
            "warnings": warnings,
        },
        "context": copy.deepcopy(context),
        "offer_facts": offer.model_dump(mode="json"),
        "offer_refs": offer_references(offer),
        "offer_fingerprint": fingerprint(offer.model_dump(mode="json")),
        "extraction_provenance": copy.deepcopy(extraction_provenance or {}),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    return public, diagnostics
