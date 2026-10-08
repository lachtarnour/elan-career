"""Calculate a score from model levels and retain advisory evidence diagnostics."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from smartapply.llm.fit_context import (
    DECISION_LEVELS,
    LEGACY_POLICY,
    POLICY,
    fingerprint,
    fit_schema,
    fit_source,
    offer_references,
    resolve,
)
from smartapply.llm.fit_response import (
    FitDecisionResponse,
    annotation_dicts,
    annotation_view,
    dimension_annotations,
    read_fit_response,
    schema_observations,
)
from smartapply.llm.fit_validation import AXES, POSITIVE, inspect_evaluation
from smartapply.llm.schemas import JobFitAssessment, OfferAnalysis


def calculate_fit_score(
    missions: int, essentials: int, responsibilities: int, issue_codes: list[str]
) -> tuple[float, dict]:
    levels = (missions, essentials, responsibilities)
    if any(type(level) is not int or not 0 <= level <= 4 for level in levels):
        raise ValueError("Invalid dimension level")
    codes = {code for code in issue_codes if isinstance(code, str)}
    raw = (
        sum(
            Decimal(weight * level)
            for weight, level in zip(LEGACY_POLICY["weights"], levels, strict=True)
        )
        / 400
    )
    caps = {
        code: Decimal(LEGACY_POLICY["caps"][code])
        for code in sorted(codes)
        if code in LEGACY_POLICY["caps"]
    }
    if missions <= 1:
        caps["weak_mission_support"] = Decimal(LEGACY_POLICY["guards"]["missions_le_1"])
    if min(levels) <= 2:
        caps["partial_dimension_support"] = Decimal(LEGACY_POLICY["guards"]["any_dimension_le_2"])
    capped = min([raw, *caps.values()])
    score = capped.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(score), {
        "policy_version": LEGACY_POLICY["version"],
        "calculation_version": LEGACY_POLICY["calculation_version"],
        "raw_score": str(raw),
        "applied_caps": {k: str(v) for k, v in caps.items()},
        "limiting_caps": [k for k, v in caps.items() if v == capped and v < raw],
        "final_score": str(score),
    }


def calculate_fit_decisions(decisions, preference_conflict) -> tuple[float, dict]:
    """One table controls levels and caps; annotations never enter this function."""
    if len(decisions) != 3 or any(state not in DECISION_LEVELS for state in decisions):
        raise ValueError("Invalid fit decisions")
    if preference_conflict not in {"none", "unknown", "confirmed"}:
        raise ValueError("Invalid preference conflict decision")
    levels = tuple(DECISION_LEVELS[state] for state in decisions)
    raw = (
        sum(
            Decimal(weight * level) for weight, level in zip(POLICY["weights"], levels, strict=True)
        )
        / 400
    )
    caps = {}
    if levels[0] <= 1:
        caps["weak_mission_support"] = Decimal(POLICY["guards"]["missions_le_1"])
    if min(levels) <= 2:
        caps["partial_dimension_support"] = Decimal(POLICY["guards"]["any_dimension_le_2"])
    if "credible_material_question" in decisions:
        caps["material_core_question"] = Decimal(POLICY["caps"]["material_core_question"])
    if preference_conflict == "confirmed":
        caps["confirmed_preference_conflict"] = Decimal(
            POLICY["caps"]["confirmed_preference_conflict"]
        )
    capped = min([raw, *caps.values()])
    score = capped.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(score), {
        "policy_version": POLICY["version"],
        "calculation_version": POLICY["calculation_version"],
        "used_decisions": dict(zip(AXES, decisions, strict=True)),
        "preference_conflict": preference_conflict,
        "raw_score": str(raw),
        "applied_caps": {key: str(value) for key, value in caps.items()},
        "limiting_caps": [key for key, value in caps.items() if value == capped and value < raw],
        "final_score": str(score),
    }


def evaluate_fit(
    evaluation: Any,
    context: dict,
    offer: OfferAnalysis,
    *,
    extraction_provenance: dict | None = None,
) -> tuple[JobFitAssessment, dict]:
    expected = POLICY if context.get("schema_version") == "job_fit_evaluation_v2" else LEGACY_POLICY
    if context["policy"] != expected:
        raise ValueError("Scoring policy has changed; this result requires explicit resumption")
    response = read_fit_response(evaluation, schema=fit_schema(context))
    if isinstance(response, FitDecisionResponse):
        score, calculation = calculate_fit_decisions(
            response.decisions, response.preference_conflict
        )
    else:
        score, calculation = calculate_fit_score(*response.levels, list(response.issue_codes))
    warnings = list(response.observations)

    # Permanent boundary: future diagnostic checks/converters may report errors,
    # but cannot veto a score, change its levels/codes or cause a provider retry.
    def observe(label, callback, fallback):
        try:
            return callback()
        except Exception as exc:
            warnings.append(f"{label}: {type(exc).__name__}: {exc}")
            return fallback

    def collect(label, callback):
        def checked():
            items = callback()
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ValueError("Diagnostic check did not return a list of observations")
            return items

        warnings.extend(observe(label, checked, []))

    collect("Annotation schema inspection", lambda: schema_observations(copy.deepcopy(response)))
    collect(
        "Evidence inspection",
        lambda: inspect_evaluation(
            annotation_view(copy.deepcopy(response)),
            {**copy.deepcopy(context), "offer_source": fit_source(extraction_provenance)},
            offer.model_copy(deep=True),
        ),
    )

    def convert_explanations():
        converted = _explanations(
            copy.deepcopy(response.raw), copy.deepcopy(context), offer.model_copy(deep=True)
        )
        if (
            not isinstance(converted, tuple)
            or len(converted) != 2
            or any(
                not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                for items in converted
            )
        ):
            raise ValueError("Explanation conversion did not return two lists of text")
        return converted

    reasons, risks = observe(
        "Explanation conversion", convert_explanations, ([], list(offer.offer_risks))
    )
    public = JobFitAssessment(fit_score=score, match_reasons=reasons[:5], risks=risks)
    diagnostics = {
        **calculation,
        "evaluation": copy.deepcopy(response.raw),
        "original_response": response.original_text,
        "used_levels": dict(zip(AXES, response.levels, strict=True)),
        "recognized_issue_codes": sorted(
            set(response.issue_codes)
            & (set(LEGACY_POLICY["caps"]) | {"limited_question", "offer_clarity"})
        ),
        "diagnostic_checks": {
            "mode": "advisory",
            "status": "warnings" if warnings else "ok",
            "warnings": list(dict.fromkeys(warnings)),
        },
        "context": copy.deepcopy(context),
        "offer_facts": offer.model_dump(mode="json"),
        "offer_refs": offer_references(offer),
        "offer_fingerprint": fingerprint(offer.model_dump(mode="json")),
        "extraction_provenance": copy.deepcopy(extraction_provenance or {}),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(response, FitDecisionResponse):
        source = fit_source(extraction_provenance)
        diagnostics["offer_source"] = source
        diagnostics["source_fingerprint"] = source["fingerprint"]
    return public, diagnostics


def _explanations(raw: dict, context: dict, offer: OfferAnalysis) -> tuple[list[str], list[str]]:
    """Use available text; never invent missing reasons or repair model judgements."""
    reasons = []
    by_axis = []
    for axis in AXES:
        axis_reasons = []
        for finding in annotation_dicts(dimension_annotations(raw, axis).get("findings")):
            support = finding.get("support")
            if isinstance(support, str) and support in POSITIVE:
                text = finding.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                text = text.strip()
                prefix = {"declared_only": "Déclaré : ", "transferable": "Transférable : "}.get(
                    support, ""
                )
                if support == "partial_support":
                    prefix = "Appui partiel, à confirmer : "
                refs = [
                    context["candidate_refs"][ref]
                    for ref in _references(finding.get("candidate_refs"))
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
                    axis_reasons.append(prefix + text)
        by_axis.append(axis_reasons)
    if "decision" in raw:
        first = list(dict.fromkeys(items[0] for items in by_axis if items))
        reasons = first + [text for text in reasons if text not in first]
    risks = list(offer.offer_risks)
    for issue in annotation_dicts(raw.get("issues")):
        text = issue.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        origin, axis = issue.get("origin"), issue.get("axis")
        prefix = (
            {
                "offer_information": "Offre à clarifier : ",
                "preference_alignment": "Orientation/préférence : ",
                "candidate_evidence": "Appui candidat à préciser : ",
            }.get(origin, "")
            if isinstance(origin, str)
            else ""
        )
        if origin == "candidate_evidence" and isinstance(axis, str) and axis in AXES:
            related = [
                finding.get("support")
                for finding in annotation_dicts(dimension_annotations(raw, axis).get("findings"))
                if set(_references(finding.get("offer_refs")))
                & set(_references(issue.get("offer_refs")))
            ]
            if "contradicted" in related:
                prefix = "Écart établi : "
            elif "not_documented" in related:
                prefix = "Non documenté : "
        text = prefix + text.strip()
        if text not in risks:
            risks.append(text)
    return reasons, list(dict.fromkeys(risks))


def _references(value) -> list[str]:
    return [ref for ref in value if isinstance(ref, str)] if isinstance(value, list) else []
