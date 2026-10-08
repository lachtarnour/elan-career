"""Immutable factual inputs and versioned policy for evidence-based fit."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from typing import Any

from smartapply.config import get_settings
from smartapply.llm.prompts.loader import load_prompt
from smartapply.llm.schemas import JobFitEvaluation, OfferAnalysis
from smartapply.profile import Bullet, Profile

POLICY_VERSION = "evidence_fit_v5"
CALCULATION_VERSION = "evidence_fit_calculation_v1"
POLICY = {
    "version": POLICY_VERSION,
    "calculation_version": CALCULATION_VERSION,
    "weights": [50, 30, 20],
    "caps": {
        "confirmed_constraint_conflict": "0.19",
        "distant_core_work": "0.39",
        "substantial_core_gap": "0.59",
        "unclear_core_work": "0.59",
        "material_core_question": "0.79",
    },
    "guards": {"missions_le_1": "0.39", "any_dimension_le_2": "0.59"},
    "rounding": "ROUND_HALF_UP:0.01",
    "diagnostic_checks": "advisory",
}


def fingerprint(value: Any) -> str:
    def serialize(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"Unsupported fingerprint value: {type(item).__name__}")

    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=serialize
        ).encode()
    ).hexdigest()


def _bullet_facts(bullet: Bullet) -> dict[str, Any]:
    return {
        "id": bullet.id,
        "text": bullet.text,
        "evidence_level": bullet.evidence_level,
        "allowed_claims": bullet.allowed_claims,
        "numbers": bullet.numbers,
    }


def candidate_facts(profile: Profile) -> dict[str, Any]:
    """Keep all factual entries in profile order, without CV selection policies."""
    return {
        "identity": {
            "title": profile.identity.title,
            "summary": profile.identity.summary,
            "residence": profile.identity.location,
        },
        "declared_skills": [
            {"category": category.name, "skills": category.skills}
            for category in profile.skills.categories
        ],
        "preferences": profile.preferences.model_dump(mode="json"),
        "experiences": [
            {
                "id": experience.id,
                "title": experience.title,
                "company": experience.company,
                "location": experience.location,
                "start_date": experience.start_date,
                "end_date": experience.end_date,
                "bullets": [_bullet_facts(bullet) for bullet in experience.bullets],
            }
            for experience in profile.experiences
        ],
        "projects": [
            {
                "id": project.id,
                "name": project.name,
                "status": project.status,
                "bullets": [_bullet_facts(bullet) for bullet in project.bullets],
            }
            for project in profile.projects
        ],
        "education": [
            degree.model_dump(mode="json", exclude={"url"}) for degree in profile.education
        ],
        "languages": [language.model_dump(mode="json") for language in profile.languages],
        "certificates": [
            certificate.model_dump(mode="json", exclude={"url"})
            for certificate in profile.certificates
        ],
    }


def candidate_references(facts: dict) -> dict:
    """Catalog paths and source types only; original texts live in facts once."""
    refs = {}

    def add(identifier, path, kind):
        if identifier in refs:
            raise ValueError(f"Duplicate evidence identifier: {identifier}")
        refs[identifier] = {"path": path, "source_type": kind}

    for key in facts["identity"]:
        add(f"identity:{key}", ["identity", key], "identity")
    for index, category in enumerate(facts["declared_skills"]):
        for skill_index, _ in enumerate(category["skills"]):
            add(
                f"declared_skill:{index}:{skill_index}",
                ["declared_skills", index, "skills", skill_index],
                "declared_skill",
            )
    for group in ("experiences", "projects"):
        for index, entry in enumerate(facts[group]):
            for bullet_index, bullet in enumerate(entry["bullets"]):
                add(f"bullet:{bullet['id']}", [group, index, "bullets", bullet_index], "bullet")
    for group in ("education", "languages", "certificates"):
        for index, entry in enumerate(facts[group]):
            identifier = entry.get("id", index)
            add(f"{group}:{identifier}", [group, index], group)
    for field, values in facts["preferences"].items():
        for index, _ in enumerate(values):
            add(f"preference:{field}:{index}", ["preferences", field, index], "preference")
    return refs


def resolve(facts: dict, reference: dict):
    value = facts
    for component in reference["path"]:
        value = value[component]
    return value


def offer_references(offer: OfferAnalysis) -> dict:
    refs = {}
    for field in (
        "main_tasks",
        "required_skills",
        "nice_to_have",
        "constraints",
        "supporting_excerpts",
        "offer_risks",
    ):
        for index, _ in enumerate(getattr(offer, field)):
            refs[f"{field}[{index}]"] = {"path": [field, index], "source_type": field}
    for field in (
        "role_type",
        "seniority",
        "domain",
        "extracted_company_name",
        "extracted_location",
        "company_context",
        "offer_language",
    ):
        refs[field] = {"path": [field], "source_type": field}
    return refs


def build_fit_context(profile: Profile, model: str, *, max_completion_tokens=None) -> dict:
    from smartapply.llm.openai_provider import _to_strict_schema

    settings = get_settings()
    facts = copy.deepcopy(candidate_facts(profile))
    result = {
        "policy": json.loads(json.dumps(POLICY)),
        "candidate_facts": facts,
        "candidate_refs": candidate_references(facts),
        "declared_experience": settings.declared_experience,
        "declared_experience_source": "settings.declared_experience",
        "system_prompt": load_prompt("job_fit/system.j2"),
        "user_template": load_prompt("job_fit/user.j2"),
        "prompt_version": "evidence_fit_v5",
        "schema_version": "job_fit_evaluation_v1",
        "schema": _to_strict_schema(JobFitEvaluation),
        "model": model,
        "temperature": 0.2,
        "configured_context_window": settings.openai_context_window_tokens,
        "max_completion_tokens": max_completion_tokens or settings.fit_max_completion_tokens,
    }
    result["candidate_fingerprint"] = fingerprint(facts)
    result["declared_experience_fingerprint"] = fingerprint(
        {
            "source": result["declared_experience_source"],
            "text": result["declared_experience"],
        }
    )
    result["prompt_fingerprint"] = fingerprint(
        {
            "system": result["system_prompt"],
            "user_template": result["user_template"],
        }
    )
    result["schema_fingerprint"] = fingerprint(result["schema"])
    result["policy_fingerprint"] = fingerprint(result["policy"])
    result["context_fingerprint"] = fingerprint(result)
    return result


def context_is_current(context: dict, profile: Profile) -> bool:
    unsigned = {key: value for key, value in context.items() if key != "context_fingerprint"}
    if fingerprint(unsigned) != context.get("context_fingerprint"):
        return False
    current = build_fit_context(profile, context["model"])
    return current["context_fingerprint"] == context["context_fingerprint"]


def build_fit_user(context: dict, offer: OfferAnalysis) -> str:
    # Render the frozen template, rather than reloading it halfway through a Batch.
    from smartapply.llm.prompts.loader import _environment

    return (
        _environment()
        .from_string(context["user_template"])
        .render(
            scoring_policy_version=context["policy"]["version"],
            candidate_json=json.dumps(context["candidate_facts"], ensure_ascii=False),
            declared_experience=context["declared_experience"],
            declared_experience_source=context["declared_experience_source"],
            candidate_reference_json=json.dumps(context["candidate_refs"], ensure_ascii=False),
            offer_json=offer.model_dump_json(),
            offer_reference_json=json.dumps(offer_references(offer), ensure_ascii=False),
            coverage_checklist_json=json.dumps(
                {
                    field: [f"{field}[{index}]" for index, _ in enumerate(getattr(offer, field))]
                    for field in ("main_tasks", "required_skills", "constraints")
                },
                ensure_ascii=False,
            ),
        )
    )
