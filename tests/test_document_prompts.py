"""Source scope and strict output contracts for final-document drafting/review."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartapply.cv.assembly import profile_experiences
from smartapply.llm import (
    AdaptedCV,
    DocumentReviewIssue,
    DocumentTextReview,
    JobAnalysis,
    MotivationLetter,
    SkillSelectionBlock,
)
from smartapply.llm.prompts import cv_adaptation, document_review, motivation_letter
from smartapply.llm.prompts.document_context import (
    allowed_source_ids,
    build_document_context,
)
from smartapply.profile import Profile


@pytest.fixture
def source_documents():
    profile = Profile.model_validate(
        {
            "identity": {
                "full_name": "Example Candidate",
                "title": "Data Scientist",
                "summary": "Works on applied machine learning.",
                "location": "Paris",
                "email": "candidate@example.com",
            },
            "preferences": {
                "target_roles": ["Data Scientist"],
                "accepted_contract_types": ["CDI"],
                "accepted_remote_policies": ["hybrid"],
            },
            "skills": {
                "categories": [{"id": "tools", "name": "Tools", "skills": ["Python", "Spark"]}]
            },
            "experiences": [
                {
                    "id": "exp_speech",
                    "company": "Speech Lab",
                    "title": "Data Scientist",
                    "location": "Paris",
                    "start_date": "01/2023",
                    "end_date": "12/2024",
                    "bullets": [
                        {
                            "id": "blt_speech",
                            "text": "Built a transcription pipeline with Whisper.",
                            "allowed_claims": ["Used Whisper for transcription at Speech Lab."],
                            "evidence_level": "verified",
                        }
                    ],
                }
            ],
            "projects": [
                {
                    "id": "proj_selected",
                    "name": "Selected Retrieval Project",
                    "status": "Ongoing",
                    "bullets": [
                        {
                            "id": "blt_selected",
                            "text": "Built a retrieval prototype.",
                            "allowed_claims": [
                                "Built a retrieval prototype.",
                                "Measured retrieval separately from answer quality.",
                                "Used an API for generation.",
                                "FULL_FINAL_CLAIM_SENTINEL",
                            ],
                            "evidence_level": "self_reported",
                        }
                    ],
                },
                {
                    "id": "proj_unselected",
                    "name": "UNSELECTED_PROJECT_SENTINEL",
                    "bullets": [{"id": "blt_unselected", "text": "UNSELECTED_FACT_SENTINEL"}],
                },
            ],
            "languages": [{"name": "English", "level": "Proficient"}],
            "education": [
                {
                    "id": "master",
                    "title": "Master of Science",
                    "institution": "Example University",
                    "start_year": 2020,
                    "end_year": 2022,
                }
            ],
            "certificates": [
                {"id": "intro", "name": "Introduction to agents", "issuer": "Example School"}
            ],
            "style_guide": {
                "tone": "precise",
                "voice": "concise",
                "dont": ["CV_ONLY_NO_FIRST_PERSON_SENTINEL"],
                "max_summary_length": 360,
                "max_summary_lines": 2,
            },
        }
    )
    analysis = JobAnalysis(
        role_type="AI Engineer",
        seniority="mid",
        domain="software",
        main_tasks=["Build useful applications"],
        required_skills=["Python"],
        nice_to_have=[],
        cv_keywords_to_include=[],
        match_reasons=["ANALYSIS_REASONING_SENTINEL"],
        risks=["ANALYSIS_RISK_SENTINEL"],
    )
    cv = AdaptedCV(
        cv_title="Data Scientist",
        professional_summary="Built a transcription pipeline with Whisper.",
        selected_experiences=profile_experiences(profile),
        selected_project_ids=["proj_selected"],
        selected_skills=[SkillSelectionBlock(category_id="tools", skills=["Python"])],
        skills_order=["tools"],
        warnings=["AUTHOR_REASONING_SENTINEL"],
    )
    return {
        "profile": profile,
        "analysis": analysis,
        "adapted_cv": cv,
        "job_title": "AI Engineer",
        "job_company": "Example Employer",
        "job_description": "Original offer context. " * 300 + "FULL_OFFER_END_SENTINEL",
        "language": "fr",
    }


def test_shared_context_keeps_declared_skills_and_selected_facts_separate(source_documents):
    before_profile = source_documents["profile"].model_dump()
    before_cv = source_documents["adapted_cv"].model_dump()
    context = build_document_context(**source_documents)

    assert context["declared_skills"]["categories"][0]["skills"] == ["Python", "Spark"]
    assert context["final_cv"]["data"]["selected_skills"][0]["skills"] == ["Python"]
    assert context["experience_evidence"][0]["bullets"][0]["text"] == (
        "Built a transcription pipeline with Whisper."
    )
    projects = context["selected_project_evidence"]
    assert [project["id"] for project in projects] == ["proj_selected"]
    assert projects[0]["bullets"][0]["allowed_claims"][-1] == "FULL_FINAL_CLAIM_SENTINEL"
    assert projects[0]["bullets"][0]["evidence_level"] == "self_reported"
    assert projects[0]["status"] == "Ongoing"
    assert context["job_offer"]["full_description"] == source_documents["job_description"]
    assert context["summary_style"]["max_characters"] == 360
    assert context["languages"][0]["level"] == "Proficient"
    assert context["education"][0]["id"] == "master"
    assert context["certificates"][0]["id"] == "intro"
    assert source_documents["profile"].model_dump() == before_profile
    assert source_documents["adapted_cv"].model_dump() == before_cv


def test_source_allowlist_matches_exposed_ids_and_excludes_unselected_projects(source_documents):
    context = build_document_context(**source_documents)

    def collect_ids(value):
        found = set()
        if isinstance(value, dict):
            if "source_id" in value:
                found.add(value["source_id"])
            for child in value.values():
                found.update(collect_ids(child))
        elif isinstance(value, list):
            for child in value:
                found.update(collect_ids(child))
        return found

    # The CV also uses source_id for its copied experiences/bullets. They are
    # document references, while the review allowlist addresses source records.
    sources_only = {key: value for key, value in context.items() if key != "final_cv"}
    exposed = collect_ids(sources_only) | {"document:final_cv"}
    allowed = allowed_source_ids(source_documents["profile"], source_documents["adapted_cv"])
    assert exposed == allowed == set(context["source_index"])
    assert "project:proj_selected" in allowed
    assert "bullet:blt_selected" in allowed
    assert "project:proj_unselected" not in allowed
    assert "bullet:blt_unselected" not in allowed


def test_letter_prompt_receives_full_offer_without_cv_only_voice_rules(source_documents):
    prompt = motivation_letter.build_user_prompt(**source_documents)
    assert "FULL_OFFER_END_SENTINEL" in prompt
    assert '"Spark"' in prompt
    assert "Whisper" in prompt
    assert "FULL_FINAL_CLAIM_SENTINEL" in prompt
    assert "UNSELECTED_FACT_SENTINEL" not in prompt
    assert "CV_ONLY_NO_FIRST_PERSON_SENTINEL" not in prompt
    assert "AUTHOR_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_RISK_SENTINEL" not in prompt
    assert "first person" in motivation_letter.SYSTEM
    assert "without requiring a supporting project" in motivation_letter.SYSTEM


@pytest.mark.parametrize("allow_corrections", [True, False])
def test_review_prompt_is_independent_and_explicit_about_correction_mode(
    source_documents, allow_corrections
):
    prompt = document_review.build_user_prompt(
        **source_documents,
        letter=MotivationLetter(subject="Candidate application", body="CURRENT_LETTER_SENTINEL"),
        validation_issues=["letter_too_short:1"],
        allow_corrections=allow_corrections,
    )
    assert "CURRENT_LETTER_SENTINEL" in prompt
    assert "FULL_OFFER_END_SENTINEL" in prompt
    assert "FULL_FINAL_CLAIM_SENTINEL" in prompt
    assert "CV_ONLY_NO_FIRST_PERSON_SENTINEL" in prompt
    assert "AUTHOR_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_RISK_SENTINEL" not in prompt
    assert "UNSELECTED_FACT_SENTINEL" not in prompt
    assert "letter_too_short:1" in prompt
    assert ("CORRECTIONS ALLOWED" in prompt) is allow_corrections
    assert ("CORRECTIONS DISABLED" in prompt) is not allow_corrections


def test_review_schema_requires_nullable_fields_and_restricts_editable_fields():
    schema = DocumentTextReview.model_json_schema()
    assert set(schema["required"]) == {
        "issues",
        "professional_summary",
        "motivation_letter_subject",
        "motivation_letter_body",
    }
    empty = DocumentTextReview(
        issues=[],
        professional_summary=None,
        motivation_letter_subject=None,
        motivation_letter_body=None,
    )
    assert empty.issues == []
    with pytest.raises(ValidationError):
        DocumentTextReview.model_validate({"issues": []})
    with pytest.raises(ValidationError):
        DocumentReviewIssue(
            field="selected_skills", passage="Python", reason="Change skill", source_ids=[]
        )
    with pytest.raises(ValidationError):
        DocumentTextReview.model_validate({**empty.model_dump(), "selected_project_ids": []})


def test_cv_prompt_accepts_declared_competence_without_attributing_a_realization(source_documents):
    profile = source_documents["profile"]
    prompt = cv_adaptation.build_user_prompt(
        profile=profile,
        analysis=source_documents["analysis"],
        job_title=source_documents["job_title"],
        job_company=source_documents["job_company"],
        selected_experiences=profile.experiences,
        selected_projects=profile.projects,
    )
    assert "General competence may rely on declared allowed_skills" in prompt
    assert "without mandatory project or experience proof" in prompt
    assert "employer/project attributions" in prompt
    assert "skills must be exact skill names" in prompt
    assert "General competence claims may rely" in cv_adaptation.SYSTEM
    assert "Specific realizations" in cv_adaptation.SYSTEM


def test_review_summary_correction_preserves_frozen_skills_without_restricting_letter():
    assert "do not introduce a declared catalogue skill" in document_review.SYSTEM
    assert "final CV's selected_skills" in document_review.SYSTEM
    assert "source-backed tool absent from the display catalogue" in document_review.SYSTEM
    assert "letter may use the entire declared catalogue" in document_review.SYSTEM
