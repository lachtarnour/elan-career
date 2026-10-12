"""Synthetic structured diagnostics for workflow tests, not semantic evaluations."""

import json

from smartapply.config import get_llm_settings
from smartapply.llm.schemas import JobFitDecisionEvaluation
from smartapply.llm_policy import resolve_route
from smartapply.pipeline.process.scope_screening import scope_context


def screening_context():
    settings = get_llm_settings()
    route = resolve_route(settings, "job_scope_screening")
    return scope_context(
        provider=route.provider,
        model=route.model,
        settings=settings,
        max_completion_tokens=route.max_output_tokens,
        effort=route.reasoning,
    )


def decision_response(*, states=("strong_fit", "strong_fit", "credible_fit")):
    return {
        "decision": {
            **dict(zip(("missions", "essentials", "responsibilities"), states, strict=True)),
        }
    }


def evaluation_from_user(user, *, levels=(4, 3, 3), reasons=None):
    def block(header):
        return json.loads(user.split(header + "\n", 1)[1].split("\n\n===", 1)[0].strip())

    candidate = block("=== CANDIDATE EVIDENCE REFERENCES JSON ===")
    offer = block("=== IMMUTABLE OFFER ANALYSIS JSON ===")
    bullet = next(ref for ref, entry in candidate.items() if entry["source_type"] == "bullet")
    skill = next(
        ref for ref, entry in candidate.items() if entry["source_type"] == "declared_skill"
    )

    def finding(
        ref, support="direct", candidate_ref=bullet, text="Réalisation documentée pertinente"
    ):
        return {
            "offer_refs": [ref],
            "candidate_refs": [candidate_ref],
            "support": support,
            "text": text,
        }

    tasks = [
        finding(f"main_tasks[{i}]", text=(reasons or ["Travail documenté"])[0])
        for i, _ in enumerate(offer["main_tasks"])
    ]
    essentials = [
        finding(f"required_skills[{i}]", "declared_only", skill, "Compétence déclarée")
        for i, _ in enumerate(offer["required_skills"])
    ]
    essentials.extend(finding(f"constraints[{i}]") for i, _ in enumerate(offer["constraints"]))
    if not essentials:
        essentials = tasks[:1]
    if not tasks:
        # Fixtures with no offer work should supply an explicit partial diagnostic instead.
        raise ValueError("Synthetic workflow fixture needs at least one main task")
    dimensions = {
        axis: {"level": level, "findings": findings}
        for axis, level, findings in zip(
            ("missions", "essentials", "responsibilities"),
            levels,
            (tasks, essentials, tasks[:1]),
            strict=True,
        )
    }
    issues = []
    for axis, level in zip(dimensions, levels, strict=True):
        if level <= 2:
            code = (
                "distant_core_work" if axis == "missions" and level <= 1 else "substantial_core_gap"
            )
            issues.append(
                {
                    "code": code,
                    "axis": axis,
                    "origin": "candidate_evidence",
                    "offer_refs": dimensions[axis]["findings"][0]["offer_refs"],
                    "candidate_refs": [],
                    "text": "Appui central partiel dans cette fixture",
                }
            )
    states = (
        "very_low_support",
        "weak_support",
        "partial_core_fit",
        "credible_fit",
        "strong_fit",
    )
    return JobFitDecisionEvaluation.model_validate(
        {
            "decision": {
                **{axis: states[level] for axis, level in zip(dimensions, levels, strict=True)},
            },
            "dimensions": {
                axis: {"findings": item["findings"]} for axis, item in dimensions.items()
            },
            "issues": [
                {key: value for key, value in item.items() if key != "code"} for item in issues
            ],
            "reliability": {
                "offer_information": "Activités et capacités attendues documentées",
                "candidate_information": "Réalisations et compétences renseignées",
                "traceability": "Appuis reliés aux références fournies",
                "business_uncertainties": [],
            },
            "neutral_offer_refs": [],
            "offer_interpretations": [],
        }
    )
