"""Summary and letter are written together after the CV structure is frozen."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from smartapply.cv.assembly import profile_experiences
from smartapply.cv.letter_writer import ApplicationTextWriter
from smartapply.llm import (
    AdaptedCV,
    ApplicationTexts,
    AssembledCV,
    CVSelection,
    JobAnalysis,
    LLMError,
    MockLLMProvider,
    MotivationLetter,
    SkillSelectionBlock,
)
from smartapply.profile import get_profile


@pytest.fixture
def text_inputs():
    profile = get_profile().model_copy(deep=True)
    cv = AssembledCV(
        cv_title="Data Scientist",
        selected_certificate_ids=[],
        selected_experiences=profile_experiences(profile),
        selected_project_ids=["proj_evidence_rag", "proj_ner_camembert", "proj_smartapply"],
        skills_profile_id="mixed",
        selected_skills=[SkillSelectionBlock(category_id="data_analysis", skills=["Python"])],
        skills_order=["data_analysis"],
        warnings=["Preserve the approved editorial note"],
    )
    analysis = JobAnalysis(
        role_type="Data Scientist",
        seniority="mid",
        domain="Research",
        main_tasks=["Analyze research data"],
        required_skills=["Python"],
        nice_to_have=[],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=["research"],
    )
    context = dict(
        job_title="Data Scientist",
        job_company="Example Research",
        job_description="Analyze research data and evaluate model reliability.",
        language="fr",
        job_id=42,
        refresh_cache=True,
    )
    return profile, cv, analysis, context


def test_selection_schema_has_no_summary_and_text_schema_cannot_reselect_cv(text_inputs):
    _, cv, _, _ = text_inputs
    selection = cv.model_dump(exclude={"selected_experiences"})
    assert "professional_summary" not in CVSelection.model_json_schema()["properties"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CVSelection.model_validate({**selection, "professional_summary": "Premature summary."})

    text_fields = {
        "professional_summary": "Data Scientist analyzing research data.",
        "motivation_letter_subject": "Application",
        "motivation_letter_body": "I would like to discuss this research role.",
    }
    assert set(ApplicationTexts.model_fields) == set(text_fields)
    for forbidden in (
        "cv_title",
        "selected_project_ids",
        "selected_skills",
        "selected_experiences",
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ApplicationTexts.model_validate({**text_fields, forbidden: cv.model_dump()[forbidden]})


def test_second_call_sees_frozen_cv_and_only_adds_summary_without_adding_skills(text_inputs):
    profile, cv, analysis, context = text_inputs
    before = cv.model_dump()
    profile_before = profile.model_dump()
    # SQL is declared in the profile but deliberately absent from the approved
    # Skills section: mentioning it in the summary must not rebuild that section.
    texts = ApplicationTexts(
        professional_summary="Data Scientist using Python and SQL to analyze research data.",
        motivation_letter_subject="Candidature Data Scientist",
        motivation_letter_body="Je souhaite contribuer à vos travaux de recherche.",
    )
    provider = MockLLMProvider({"application_texts": texts})
    provider.complete_json = Mock(wraps=provider.complete_json)
    writer = ApplicationTextWriter(profile, provider, reference_date=date(2026, 9, 21))

    final_cv, letter = writer.generate_texts(cv, analysis, **context)

    assert type(final_cv) is AdaptedCV
    assert final_cv.model_dump(exclude={"professional_summary"}) == before
    assert final_cv.professional_summary == texts.professional_summary
    assert letter == MotivationLetter(
        subject=texts.motivation_letter_subject,
        body=texts.motivation_letter_body,
    )
    assert cv.model_dump() == before
    assert profile.model_dump() == profile_before
    assert final_cv.selected_skills[0].skills == ["Python"]
    call = provider.complete_json.call_args.kwargs
    assert provider.complete_json.call_count == 1
    assert call["purpose"] == "application_texts"
    assert call["schema"] is ApplicationTexts
    assert call["model"] == provider.smart_model
    assert call["refresh_cache"] is True
    assert call["job_id"] == 42
    source_position = call["user"].index('"source_index"')
    context_start = call["user"].rfind("{", 0, source_position)
    sources, _ = json.JSONDecoder().raw_decode(call["user"][context_start:])
    assert sources["final_cv"]["data"] == cv.model_dump(
        exclude={"warnings", "project_selection_reasons"}
    )
    assert "professional_summary" not in sources["final_cv"]["data"]
    assert sources["job_offer"]["full_description"] == context["job_description"]
    assert [project["id"] for project in sources["selected_project_evidence"]] == (
        cv.selected_project_ids
    )


@pytest.mark.parametrize("invalid_part", ["projects", "experiences"])
def test_invalid_cv_structure_prevents_second_llm_call(text_inputs, invalid_part):
    profile, cv, analysis, context = text_inputs
    if invalid_part == "projects":
        cv.selected_project_ids = ["proj_evidence_rag"]
    else:
        cv.selected_experiences[0].bullets[0].text = "An unsupported rewritten experience."
    provider = MockLLMProvider({})
    provider.complete_json = Mock(side_effect=AssertionError("Invalid CV reached text generation"))
    writer = ApplicationTextWriter(profile, provider)

    with pytest.raises(LLMError):
        writer.generate_texts(cv, analysis, **context)

    provider.complete_json.assert_not_called()
