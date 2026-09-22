"""Project policy and selected-evidence boundaries in both drafting prompts."""

from __future__ import annotations

import pytest

from smartapply.cv.assembly import assemble_cv
from smartapply.llm import AssembledCV, CVPersonalization, JobAnalysis, MotivationLetter
from smartapply.llm.prompts import (
    application_draft,
    application_texts,
    cv_adaptation,
    document_review,
    motivation_letter,
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


@pytest.mark.parametrize("builder", [cv_adaptation, application_draft])
def test_catalogue_exposes_fixed_selection_policy_without_mutating_profile(
    builder, selection_inputs
):
    before = selection_inputs["profile"].model_dump()
    prompt = builder.build_user_prompt(**selection_inputs)
    for tier in ("primary", "specialized", "disabled"):
        assert f'"tier": "{tier}"' in prompt
        assert f'"rationale": "Reviewed quality: {tier}"' in prompt
    assert '"activation_conditions": ["Named entity recognition"]' in prompt
    assert "these projects are candidates, not a final selection" in prompt
    assert selection_inputs["profile"].model_dump() == before


@pytest.mark.parametrize("builder", [cv_adaptation, application_draft])
def test_full_offer_is_available_for_exact_evidence_only_after_cacheable_prefix(
    builder, selection_inputs
):
    description = "Concevoir un système d'extraction d'entités cliniques."
    first = builder.build_user_prompt(**selection_inputs, job_description=description)
    second = builder.build_user_prompt(
        **selection_inputs,
        job_description="Build image classification models for quality control.",
    )
    assert first.count(description) == 1
    assert first.index(description) > first.index("=== JOB ===")
    assert first.split("=== JOB ===")[0] == second.split("=== JOB ===")[0]
    assert selection_inputs["analysis"].main_tasks[0] in first
    assert "Full job description (offer evidence only" not in builder.build_user_prompt(
        **selection_inputs
    )


@pytest.mark.parametrize("builder", [cv_adaptation, application_draft])
def test_drafting_prompts_share_policy_and_select_before_writing(builder, selection_inputs):
    system = builder.build_system_prompt()
    prompt = builder.build_user_prompt(**selection_inputs)
    rules = load_prompt("cv_adaptation/project_selection_rules.j2")
    assert rules in system
    assert "Choose projects BEFORE writing" in system
    assert "Select exactly 3 unique projects" in system
    assert "specialized: select at most one" in system
    assert "disabled: never select" in system
    assert "exact excerpt" in system
    assert "Selection reasons and selection_policy are internal metadata" in system
    assert "project_selection_reasons" in prompt
    if builder is application_draft:
        assert "Omit all unselected project claims" in prompt
    else:
        assert "CVSelection" in prompt
        assert "professional_summary," not in prompt
    for text in (system, prompt):
        assert "3 to 4 projects" not in text
        assert "0 to 3 unique projects" not in text
        assert "no minimum" not in text
        assert "selected alone" not in text
        assert "select all available projects" not in text


@pytest.mark.parametrize("builder", [cv_adaptation, application_draft])
def test_three_project_minimum_preserves_honest_fit_and_specialty_activation(
    builder, selection_inputs
):
    for text in (builder.build_system_prompt(), builder.build_user_prompt(**selection_inputs)):
        assert "strongest direct matches first" in text
        assert "primary projects with the best documented transferable strengths" in text
        assert "transferable relevance honestly" in text
        assert "If fewer than 3 projects can be selected under these rules" in text
        assert "reject an incomplete selection" in text
        assert "exact" in text and "excerpt" in text


def test_minimum_is_described_without_rejecting_legacy_snapshots():
    field = CVPersonalization.model_fields["selected_project_ids"]
    assert "Exactly 3 unique" in field.description
    assert "transferable strengths" in field.description
    for count in (0, 1, 2, 3, 4):
        old_snapshot = CVPersonalization(
            selected_project_ids=[f"legacy_{index}" for index in range(count)],
            cv_title="Data Scientist",
            professional_summary="Works on applied NLP.",
            skills_order=[],
            warnings=[],
        )
        assert len(old_snapshot.selected_project_ids) == count


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
        adapted.model_dump(exclude={"professional_summary"})
    )
    prompts = [
        motivation_letter.build_user_prompt(**shared),
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
