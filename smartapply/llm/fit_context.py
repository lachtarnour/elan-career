"""Immutable factual inputs and fingerprinted policy for evidence-based fit."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from typing import Any

from smartapply.config import Settings, get_llm_settings, get_settings
from smartapply.llm.prompts.loader import load_prompt
from smartapply.llm.schemas import JobFitDecisionEvaluation, OfferAnalysis
from smartapply.llm_policy import resolve_route
from smartapply.profile import Bullet, Profile

DECISION_LEVELS = {
    "very_low_support": 0,
    "weak_support": 1,
    "partial_core_fit": 2,
    "credible_fit": 3,
    "strong_fit": 4,
}
POLICY = {
    "name": "evidence_fit",
    "weights": [35, 45, 20],
    "decision_levels": DECISION_LEVELS,
    "rounding": "ROUND_HALF_UP:0.01",
    "diagnostic_checks": "advisory",
}


def fit_schema(context: dict):
    """Accept only the current fit generation contract."""
    return JobFitDecisionEvaluation


def fit_source(provenance: dict | None) -> dict:
    """Snapshot only the source actually associated with the retained extraction."""
    analyzer = (provenance or {}).get("analyzer_input")
    analyzer = analyzer if isinstance(analyzer, dict) else {}
    body = analyzer.get("offer_body")
    available = isinstance(body, str) and bool(body.strip())
    source = {
        "available": available,
        "reference": "offer_source:body",
        "offer_body": body if available else "",
        "metadata": {
            key: copy.deepcopy(analyzer[key])
            for key in (
                "title",
                "company",
                "location",
                "application_url",
                "source",
                "source_metadata",
            )
            if key in analyzer
        },
    }
    source["fingerprint"] = fingerprint(source)
    return source


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


def build_fit_context(
    profile: Profile,
    model: str,
    *,
    provider: str | None = None,
    max_completion_tokens=None,
    settings: Settings | None = None,
    effort: str | None = "auto",
) -> dict:
    from smartapply.llm.request_options import stage_effort
    from smartapply.llm.structured import to_strict_schema

    runtime_settings = get_settings()
    transport_settings = settings or get_llm_settings()
    if provider is None:
        from smartapply.llm_policy import CallConfigurationError

        route = resolve_route(transport_settings, "job_fit_assessment")
        if model != route.model:
            raise CallConfigurationError(
                "job_fit_assessment", "model", "le modèle demandé diffère du réglage de cet appel"
            )
        provider = route.provider
    facts = copy.deepcopy(candidate_facts(profile))
    result = {
        "policy": copy.deepcopy(POLICY),
        "candidate_facts": facts,
        "candidate_refs": candidate_references(facts),
        "declared_experience": runtime_settings.declared_experience,
        "declared_experience_source": "settings.declared_experience",
        "system_prompt": load_prompt(
            "job_fit/anthropic_system.j2" if provider == "anthropic" else "job_fit/system.j2"
        ),
        "user_template": load_prompt("job_fit/user.j2"),
        "schema": to_strict_schema(JobFitDecisionEvaluation),
        "model": model,
        "temperature": 0.2,
        "configured_context_window": transport_settings.llm_context_window(provider),
        "max_completion_tokens": max_completion_tokens
        or resolve_route(transport_settings, "job_fit_assessment").max_output_tokens,
    }
    frozen_effort = (
        stage_effort(provider, model, "job_fit_assessment", settings=transport_settings)
        if effort == "auto"
        else effort
    )
    result["configured_reasoning"] = frozen_effort if frozen_effort is not None else "auto"
    if frozen_effort is not None:
        result["effort"] = frozen_effort
    result["provider"] = provider
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
    if context.get("policy") != POLICY:
        return False
    unsigned = {key: value for key, value in context.items() if key != "context_fingerprint"}
    if fingerprint(unsigned) != context.get("context_fingerprint"):
        return False
    current = build_fit_context(
        profile,
        context["model"],
        provider=context.get("provider", "openai"),
        max_completion_tokens=context["max_completion_tokens"],
        effort=context.get("effort"),
    )
    # The former shared prompt remains an explicit supported representation for
    # signed Anthropic runs already submitted. Never rewrite their frozen text.
    if context.get("provider") == "anthropic" and context.get("system_prompt") == load_prompt(
        "job_fit/system.j2"
    ):
        current["system_prompt"] = context["system_prompt"]
        current["prompt_fingerprint"] = fingerprint(
            {
                "system": current["system_prompt"],
                "user_template": current["user_template"],
            }
        )
    # Transport settings may change while a Batch is running. The signed run
    # keeps its original model, parameters and provider; only facts, prompts,
    # schemas and policy must still match the current application contract.
    for key in ("temperature", "configured_context_window"):
        current[key] = copy.deepcopy(context[key])
    if "provider" not in context:
        # Historical OpenAI contexts were signed before the provider field was
        # persisted. Keep their original signed shape during read/import.
        current.pop("provider", None)
    if "configured_reasoning" not in context:
        current.pop("configured_reasoning", None)
    current["context_fingerprint"] = fingerprint(
        {key: value for key, value in current.items() if key != "context_fingerprint"}
    )
    return current["context_fingerprint"] == context["context_fingerprint"]


def build_fit_user(context: dict, offer: OfferAnalysis, *, extraction_provenance=None) -> str:
    # Render the frozen template, rather than reloading it halfway through a Batch.
    from smartapply.llm.prompts.loader import _environment

    return (
        _environment()
        .from_string(context["user_template"])
        .render(
            scoring_policy_json=json.dumps(context["policy"], ensure_ascii=False),
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
            source_json=json.dumps(fit_source(extraction_provenance), ensure_ascii=False),
        )
    )
