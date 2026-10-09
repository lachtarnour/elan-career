"""Project policy and selected-evidence boundaries in CV and document prompts."""

from __future__ import annotations

import pytest

from smartapply.cv.assembly import assemble_cv
from smartapply.llm import AssembledCV, CVPersonalization, JobAnalysis, MotivationLetter
from smartapply.llm.prompts import (
    application_texts,
    cv_adaptation,
    document_review,
)
from smartapply.llm.prompts.document_context import build_document_context
from smartapply.llm.prompts.loader import load_prompt
from smartapply.profile import Project, get_profile


@pytest.fixture
def selection_inputs():
    profile = get_profile().model_copy(deep=True)
    projects = []
    for tier in ("primary", "specialized", "disabled"):
        projects.append(
            Project.model_validate(
                {
                    "id": f"project_{tier}",
                    "name": f"Project {tier}",
                    "bullets": [{"id": f"bullet_{tier}", "text": "Built an NLP model."}],
                    "selection_policy": {
                        "tier": tier,
                        "rationale": f"Reviewed quality: {tier}",
                        "activation_conditions": ["Named entity recognition"]
                        if tier == "specialized"
                        else [],
                    },
                }
            )
        )
    profile.projects = projects
    return {
        "profile": profile,
        "analysis": JobAnalysis(
            role_type="Data Scientist",
            seniority="mid",
            domain="NLP",
            main_tasks=["Extract named entities from clinical records."],
            required_skills=["Named entity recognition", "Python"],
            nice_to_have=[],
            match_reasons=[],
            risks=[],
            cv_keywords_to_include=["NLP"],
        ),
        "job_title": "Data Scientist",
        "job_company": "Example",
        "selected_experiences": profile.experiences,
        "selected_projects": profile.projects,
    }


def test_catalogue_exposes_fixed_selection_policy_without_mutating_profile(selection_inputs):
    before = selection_inputs["profile"].model_dump()
    prompt = cv_adaptation.build_user_prompt(**selection_inputs)
    for tier in ("primary", "specialized", "disabled"):
        assert f'"tier": "{tier}"' in prompt
        assert f'"rationale": "Reviewed quality: {tier}"' in prompt
    assert '"activation_conditions": ["Named entity recognition"]' in prompt
    assert selection_inputs["profile"].model_dump() == before


def test_cv_selection_prompt_exposes_certificates_without_extra_call(selection_inputs):
    prompt = cv_adaptation.build_user_prompt(**selection_inputs)
    for certificate in selection_inputs["profile"].certificates:
        assert f"id={certificate.id} :: {certificate.name}" in prompt


def test_full_offer_is_available_for_exact_evidence_only_after_cacheable_prefix(selection_inputs):
    description = "Concevoir un système d'extraction d'entités cliniques."
    first = cv_adaptation.build_user_prompt(**selection_inputs, job_description=description)
    second = cv_adaptation.build_user_prompt(
        **selection_inputs,
        job_description="Build image classification models for quality control.",
    )
    assert first.count(description) == 1
    assert first.index(description) > first.index("=== JOB ===")
    assert first.split("=== JOB ===")[0] == second.split("=== JOB ===")[0]
    assert selection_inputs["analysis"].main_tasks[0] in first
    assert description not in cv_adaptation.build_user_prompt(**selection_inputs)


def test_cv_prompt_uses_canonical_selection_policy(selection_inputs):
    rules = load_prompt("cv_adaptation/project_selection_rules.j2")
    assert rules in cv_adaptation.build_system_prompt()
    assert "project_selection_reasons" in cv_adaptation.build_user_prompt(**selection_inputs)


@pytest.mark.parametrize("count", range(5))
def test_legacy_project_counts_remain_readable(count):
    selected_ids = [f"legacy_{index}" for index in range(count)]
    old_snapshot = CVPersonalization(
        selected_project_ids=selected_ids,
        cv_title="Data Scientist",
        professional_summary="Works on applied NLP.",
        skills_order=[],
        warnings=[],
    )
    assert old_snapshot.selected_project_ids == selected_ids


def test_selection_audit_stays_out_of_letter_and_independent_review_sources(selection_inputs):
    profile = selection_inputs["profile"]
    personalization = CVPersonalization(
        cv_title="Data Scientist",
        professional_summary="Works on applied NLP.",
        selected_project_ids=["project_primary"],
        project_selection_reasons=[
            {
                "project_id": "project_primary",
                "reason": "INTERNAL_SELECTION_REASON_SENTINEL",
                "offer_evidence": "Extract named entities from clinical records.",
                "complementary_value": "INTERNAL_COMPLEMENTARITY_SENTINEL",
            }
        ],
        skills_order=[],
        warnings=["INTERNAL_WARNING_SENTINEL"],
    )
    adapted = assemble_cv(personalization, profile)
    shared = {
        key: selection_inputs[key] for key in ("profile", "analysis", "job_title", "job_company")
    }
    shared.update(adapted_cv=adapted, job_description="Develop useful NLP applications.")
    context = build_document_context(**shared)
    assert context["final_cv"]["data"]["selected_project_ids"] == ["project_primary"]
    assert "project_selection_reasons" not in context["final_cv"]["data"]
    assert len(context["selected_project_evidence"]) == 1
    evidence = context["selected_project_evidence"][0]
    assert evidence["source_id"] == "project:project_primary"
    assert "selection_policy" not in evidence
    assert evidence["bullets"]

    assembled = AssembledCV.model_validate(
        {**adapted.model_dump(exclude={"professional_summary"}), "selected_certificate_ids": []}
    )
    prompts = [
        application_texts.build_user_prompt(**{**shared, "adapted_cv": assembled}),
    ]
    for allow_corrections in (True, False):
        prompts.append(
            document_review.build_user_prompt(
                **shared,
                letter=MotivationLetter(subject="Application", body="I am interested."),
                validation_issues=[],
                allow_corrections=allow_corrections,
            )
        )
    for prompt in prompts:
        assert "INTERNAL_SELECTION_REASON_SENTINEL" not in prompt
        assert "INTERNAL_COMPLEMENTARITY_SENTINEL" not in prompt
        assert "INTERNAL_WARNING_SENTINEL" not in prompt
        assert "Reviewed quality:" not in prompt
        assert "project_selection_reasons" not in prompt
        assert "selection_policy" not in prompt
        assert "project:project_primary" in prompt
        assert "project:project_disabled" not in prompt
        assert "project:project_specialized" not in prompt

    # The persistent snapshot retains the audit for inspection and reporting.
    assert adapted.project_selection_reasons == personalization.project_selection_reasons
    assert profile.projects[0].selection_policy.rationale == "Reviewed quality: primary"
