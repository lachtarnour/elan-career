"""Source scope and strict output contracts for final-document drafting/review."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from smartapply.cv.assembly import profile_experiences
from smartapply.llm import (
    AdaptedCV,
    AssembledCV,
    DocumentReviewIssue,
    DocumentTextReview,
    JobAnalysis,
    MotivationLetter,
    SkillSelectionBlock,
)
from smartapply.llm.prompts import (
    application_texts,
    cv_adaptation,
    document_review,
)
from smartapply.llm.prompts.document_context import (
    allowed_source_ids,
    build_document_context,
)
from smartapply.llm.prompts.loader import load_prompt
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


def _assembled_inputs(source_documents):
    return {
        **source_documents,
        "adapted_cv": AssembledCV.model_validate(
            {
                **source_documents["adapted_cv"].model_dump(exclude={"professional_summary"}),
                "selected_certificate_ids": [],
            }
        ),
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
    assert context["title_style"]["source_id"] == "style:title"
    assert context["title_style"]["language"] == "en"
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

    aggregate = context["candidate_evidence_scope"]
    assert aggregate["source_id"] == "candidate:provided_evidence"
    candidate_blocks = {
        key: context[key]
        for key in (
            "candidate_identity",
            "declared_skills",
            "experience_evidence",
            "selected_project_evidence",
            "languages",
            "education",
            "certificates",
        )
    }
    members = set(aggregate["member_source_ids"])
    assert members == collect_ids(candidate_blocks)
    assert members <= allowed
    assert {
        "profile:identity",
        "profile:declared_skills",
        "skills:tools",
        "experience:exp_speech",
        "bullet:blt_speech",
        "project:proj_selected",
        "bullet:blt_selected",
        "education:master",
        "certificate:intro",
        "language:0",
    } == members
    assert not members.intersection(
        {
            "candidate:provided_evidence",
            "document:final_cv",
            "offer:description",
            "offer:analysis",
            "style:title",
            "style:summary",
            "style:letter",
            "context:reference_date",
            "project:proj_unselected",
            "bullet:blt_unselected",
        }
    )


def test_candidate_aggregate_does_not_claim_to_cover_unexposed_profile_records(source_documents):
    cv = source_documents["adapted_cv"].model_copy(
        update={"selected_experiences": [], "selected_project_ids": []}
    )
    context = build_document_context(**{**source_documents, "adapted_cv": cv})
    members = context["candidate_evidence_scope"]["member_source_ids"]

    assert context["experience_evidence"] == context["selected_project_evidence"] == []
    assert "experience:exp_speech" not in members
    assert "bullet:blt_speech" not in members
    assert "project:proj_selected" not in members
    assert "bullet:blt_selected" not in members
    assert "profile:declared_skills" in members
    assert "skills:tools" in members
    assert context["declared_skills"]["categories"][0]["skills"] == ["Python", "Spark"]
    assert "candidate:provided_evidence" in context["source_index"]


def test_texts_prompt_receives_full_offer_and_assembled_cv_without_draft_summary(source_documents):
    prompt = application_texts.build_user_prompt(**_assembled_inputs(source_documents))
    assert "FULL_OFFER_END_SENTINEL" in prompt
    assert '"Spark"' in prompt
    assert "Whisper" in prompt
    assert "FULL_FINAL_CLAIM_SENTINEL" in prompt
    assert "UNSELECTED_FACT_SENTINEL" not in prompt
    assert "CV_ONLY_NO_FIRST_PERSON_SENTINEL" not in prompt
    assert "AUTHOR_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_RISK_SENTINEL" not in prompt
    start = prompt.rfind("{", 0, prompt.index('"source_index"'))
    context, _ = json.JSONDecoder().raw_decode(prompt[start:])
    assert context["final_cv"]["read_only"] is True
    assert context["final_cv"]["data"]["cv_title"] == source_documents["adapted_cv"].cv_title
    assert "title_style" not in context
    assert "professional_summary" not in context["final_cv"]["data"]
    assert context["summary_style"]["language"] == "en"
    assert context["letter_style"]["language"] == "fr"
    assert "style:summary" in context["source_index"]
    assert "style:title" not in context["source_index"]


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
    assert "CV_ONLY_NO_FIRST_PERSON_SENTINEL" not in prompt
    assert "AUTHOR_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_REASONING_SENTINEL" not in prompt
    assert "ANALYSIS_RISK_SENTINEL" not in prompt
    assert "UNSELECTED_FACT_SENTINEL" not in prompt
    assert "letter_too_short:1" in prompt
    assert ("CORRECTIONS ALLOWED" in prompt) is allow_corrections
    assert ("CORRECTIONS DISABLED" in prompt) is not allow_corrections


@pytest.mark.parametrize("allow_corrections", [True, False])
def test_reviewer_receives_title_with_explicit_cv_boundaries(source_documents, allow_corrections):
    source_documents["adapted_cv"].cv_title = "Senior Research Scientist"
    prompt = document_review.build_user_prompt(
        **source_documents,
        letter=MotivationLetter(
            subject="Application for Research Scientist", body="I am interested."
        ),
        validation_issues=[],
        allow_corrections=allow_corrections,
    )
    fields, _ = json.JSONDecoder().raw_decode(prompt.split("CURRENT TEXT FIELDS\n", 1)[1])
    assert fields == {
        "cv_title": "Senior Research Scientist",
        "professional_summary": source_documents["adapted_cv"].professional_summary,
        "motivation_letter_subject": "Application for Research Scientist",
        "motivation_letter_body": "I am interested.",
    }
    start = prompt.rfind("{", 0, prompt.index('"source_index"'))
    context, _ = json.JSONDecoder().raw_decode(prompt[start:])
    snapshot = context["final_cv"]
    assert "read_only" not in snapshot
    editable = ["cv_title", "professional_summary"] if allow_corrections else []
    assert snapshot["editable_fields"] == editable
    assert set(snapshot["read_only_fields"]) == set(snapshot["data"]) - set(editable)
    assert "style:title" in context["source_index"]
    assert context["candidate_identity"]["data"]["title"] == "Data Scientist"


def test_review_schema_requires_targeted_patch_contract_and_restricts_editable_fields():
    schema = DocumentTextReview.model_json_schema()
    assert set(schema["required"]) == {"issues"}
    empty = DocumentTextReview(issues=[])
    assert empty.issues == []
    issue = {
        "field": "motivation_letter_body",
        "category": "factual",
        "passage": "A demonstrably unsupported claim.",
        "reason": "No supplied source supports attributing this outcome to the candidate.",
        "source_ids": ["profile:declared_skills"],
        "replacement": None,
    }
    assert DocumentReviewIssue(**issue).replacement is None
    assert DocumentReviewIssue(**{**issue, "field": "cv_title"}).field == "cv_title"
    assert DocumentReviewIssue(**{**issue, "replacement": ""}).replacement == ""
    with pytest.raises(ValidationError):
        DocumentReviewIssue.model_validate(
            {key: value for key, value in issue.items() if key != "replacement"}
        )
    with pytest.raises(ValidationError):
        DocumentReviewIssue.model_validate({**issue, "field": "selected_skills"})
    with pytest.raises(ValidationError):
        DocumentReviewIssue.model_validate({**issue, "category": "approved"})
    with pytest.raises(ValidationError):
        DocumentTextReview.model_validate({**empty.model_dump(), "selected_project_ids": []})
    with pytest.raises(ValidationError):
        DocumentTextReview.model_validate({**empty.model_dump(), "professional_summary": None})


@pytest.mark.parametrize("allow_corrections", [True, False])
def test_summary_editorial_contract_is_shared_from_generation_through_confirmation(
    source_documents, allow_corrections
):
    profile = source_documents["profile"]
    profile.style_guide.do = ["Keep technical stack visible", "Quantify whenever possible"]
    profile.style_guide.max_summary_length = 200
    before = profile.model_dump()
    generation_prompt = application_texts.build_user_prompt(**_assembled_inputs(source_documents))
    review_prompt = document_review.build_user_prompt(
        **source_documents,
        letter=MotivationLetter(subject="Application", body="I would like to contribute."),
        validation_issues=[],
        allow_corrections=allow_corrections,
    )
    start = review_prompt.rfind("{", 0, review_prompt.index('"source_index"'))
    context, _ = json.JSONDecoder().raw_decode(review_prompt[start:])
    summary = context["summary_style"]
    shared_rules = load_prompt("cv_adaptation/summary_rules.j2")

    assert summary["editorial_rules"] == shared_rules
    assert shared_rules in application_texts.build_system_prompt()
    assert shared_rules not in cv_adaptation.build_system_prompt()

    assert summary["source_id"] == "style:summary"
    assert "style:summary" in context["source_index"]
    assert summary["language"] == "en"
    assert summary["tone"] == "precise"
    assert summary["voice"] == "concise"
    assert "do" not in summary
    assert "dont" not in summary
    assert summary["max_characters"] == 200
    assert summary["max_lines"] == 2
    assert "CV_ONLY_NO_FIRST_PERSON_SENTINEL" not in generation_prompt
    generation_start = generation_prompt.rfind("{", 0, generation_prompt.index('"source_index"'))
    generation_context, _ = json.JSONDecoder().raw_decode(generation_prompt[generation_start:])
    assert generation_context["summary_style"] == summary
    assert context["job_analysis"]["data"]["main_tasks"] == ["Build useful applications"]
    assert context["job_offer"]["full_description"] == source_documents["job_description"]
    assert profile.model_dump() == before


def test_combined_generation_keeps_summary_and_letter_styles_separate(source_documents):
    profile_before = source_documents["profile"].model_dump()
    prompt = application_texts.build_user_prompt(**_assembled_inputs(source_documents))
    start = prompt.rfind("{", 0, prompt.index('"source_index"'))
    context, _ = json.JSONDecoder().raw_decode(prompt[start:])

    assert "do" not in context["summary_style"]
    assert "dont" not in context["summary_style"]
    assert "CV_ONLY_NO_FIRST_PERSON_SENTINEL" not in json.dumps(context["letter_style"])
    assert "style:summary" in context["source_index"]
    # Both texts share factual sources but retain their own voice and limits.
    assert context["recruiter_text_style"]["editorial_rules"] == load_prompt(
        "shared/recruiter_text_rules.j2"
    )
    assert context["letter_style"]["voice"] == "first person: je in French, I in English"
    assert source_documents["profile"].model_dump() == profile_before


@pytest.mark.parametrize("language", ["fr", "en"])
@pytest.mark.parametrize("allow_corrections", [True, False])
def test_subject_and_body_contract_is_identical_in_generation_review_and_confirmation(
    source_documents, language, allow_corrections
):
    source_documents["language"] = language
    before_profile = source_documents["profile"].model_dump()
    before_cv = source_documents["adapted_cv"].model_dump()
    expected = build_document_context(**source_documents)
    subject = expected["letter_style"]["subject"]
    assert subject["target_role"] == source_documents["job_title"]
    assert subject["candidate_name"] == source_documents["profile"].identity.full_name

    prompts = (
        application_texts.build_user_prompt(**_assembled_inputs(source_documents)),
        document_review.build_user_prompt(
            **source_documents,
            letter=MotivationLetter(
                subject="Application – AI Engineer – Example Candidate",
                body="I would like to contribute.",
            ),
            validation_issues=[],
            allow_corrections=allow_corrections,
        ),
    )
    for prompt in prompts:
        start = prompt.rfind("{", 0, prompt.index('"source_index"'))
        exposed, _ = json.JSONDecoder().raw_decode(prompt[start:])
        assert exposed["letter_style"] == expected["letter_style"]
        assert exposed["letter_style"]["language"] == language
        # Removing unrelated bullet guidance must not remove the underlying facts,
        # their allowed claims or the ongoing/self-reported project qualifications.
        for source_block in ("experience_evidence", "selected_project_evidence", "declared_skills"):
            assert exposed[source_block] == expected[source_block]
        assert "FULL_FINAL_CLAIM_SENTINEL" in prompt
        assert "CV_ONLY_NO_FIRST_PERSON_SENTINEL" not in prompt

    assert source_documents["profile"].model_dump() == before_profile
    assert source_documents["adapted_cv"].model_dump() == before_cv


def test_document_sources_include_an_explicit_injectable_reference_date(source_documents):
    reference_date = date(2031, 4, 17)
    context = build_document_context(**source_documents, reference_date=reference_date)
    assert context["reference_date"]["date"] == "2031-04-17"
    assert context["reference_date"]["source_id"] == "context:reference_date"
    assert "context:reference_date" in context["source_index"]
    assert "2031-04-17" in application_texts.build_user_prompt(
        **_assembled_inputs(source_documents), reference_date=reference_date
    )
    assert "2031-04-17" in document_review.build_user_prompt(
        **source_documents,
        reference_date=reference_date,
        letter=MotivationLetter(subject="Application", body="I can contribute."),
        validation_issues=[],
        allow_corrections=True,
    )


def test_document_context_defaults_to_current_date(source_documents, monkeypatch):
    from smartapply.llm.prompts import document_context

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2033, 2, 8)

    monkeypatch.setattr(document_context, "date", FixedDate)
    context = build_document_context(**source_documents)
    assert context["reference_date"]["date"] == "2033-02-08"


def test_reviewer_receives_validator_counts_for_multilingual_text(source_documents):
    from smartapply.cv.motivation_validator import _paragraph_count, _word_count

    body = "J'ai construit un outil.\n\nI maintain a well-documented service.\n \nVoici ma contribution."
    summary = "First line.\n\nSecond line."
    source_documents["adapted_cv"].professional_summary = summary
    prompt = document_review.build_user_prompt(
        **source_documents,
        letter=MotivationLetter(subject="Application", body=body),
        validation_issues=["letter_too_short:12"],
        allow_corrections=True,
    )
    serialized = prompt.split("OBJECTIVE METRICS COMPUTED BY THE APPLICATION\n", 1)[1]
    metrics, _ = json.JSONDecoder().raw_decode(serialized)
    assert metrics == {
        "motivation_letter_body": {
            "word_count": _word_count(body),
            "paragraph_count": _paragraph_count(body),
        },
        "professional_summary": {"character_count": len(summary), "nonempty_line_count": 2},
    }
    assert "letter_too_short:12" in prompt


@pytest.mark.parametrize("with_analysis_context", [True, False])
def test_motivation_leads_are_explicit_shared_and_do_not_replace_offer(
    source_documents, with_analysis_context
):
    analysis = source_documents["analysis"]
    if with_analysis_context:
        analysis.company_context = "COMPANY_CONTEXT_SENTINEL from a stored analysis."
        analysis.offer_interest_points = ["TEAM_INTEREST_SENTINEL", "MISSION_INTEREST_SENTINEL"]
    before = analysis.model_dump()
    context = build_document_context(**source_documents)
    brief = context["motivation_context"]

    assert brief["source_id"] == "offer:analysis"
    assert brief["derived_from"] == "offer:description"
    assert brief["company_context"] == analysis.company_context
    assert brief["offer_interest_points"] == analysis.offer_interest_points
    assert "company_context" not in context["job_analysis"]["data"]
    assert "offer_interest_points" not in context["job_analysis"]["data"]
    assert context["job_offer"]["full_description"] == source_documents["job_description"]
    assert analysis.model_dump() == before

    prompts = [
        application_texts.build_user_prompt(**_assembled_inputs(source_documents)),
        document_review.build_user_prompt(
            **source_documents,
            letter=MotivationLetter(subject="Application", body="I would like to contribute."),
            validation_issues=[],
            allow_corrections=True,
        ),
    ]
    for prompt in prompts:
        start = prompt.rfind("{", 0, prompt.index('"source_index"'))
        exposed, _ = json.JSONDecoder().raw_decode(prompt[start:])
        assert exposed["motivation_context"] == brief
        assert exposed["job_offer"] == context["job_offer"]
        assert exposed["candidate_evidence_scope"] == context["candidate_evidence_scope"]
        assert prompt.index('"motivation_context"') < prompt.index('"final_cv"')
        if with_analysis_context:
            assert prompt.count("COMPANY_CONTEXT_SENTINEL") == 1
            assert prompt.count("TEAM_INTEREST_SENTINEL") == 1
        else:
            assert exposed["motivation_context"]["company_context"] == ""
            assert exposed["motivation_context"]["offer_interest_points"] == []


def test_conflicting_analysis_remains_identified_as_derived_not_company_evidence(source_documents):
    source_documents["analysis"].company_context = "The company sells transport products."
    source_documents["job_description"] = "Our team develops tools for agricultural research."
    context = build_document_context(**source_documents)

    assert (
        context["motivation_context"]["company_context"] == "The company sells transport products."
    )
    assert context["job_offer"]["full_description"] == source_documents["job_description"]
    assert context["motivation_context"]["source_id"] != context["job_offer"]["source_id"]
    assert context["motivation_context"]["derived_from"] == context["job_offer"]["source_id"]
    # Python keeps provenance; it does not attempt to reconcile meaning using
    # a technology/company-name dictionary or silently re-analyze the offer.


def test_letter_purpose_is_shared_between_generation_and_review(source_documents):
    from smartapply.cv.motivation_validator import MotivationLetterValidator

    context = build_document_context(**source_documents)
    rules = context["letter_style"]
    validator = MotivationLetterValidator(source_documents["profile"])
    assert (
        (rules["min_words"], rules["max_words"])
        == (
            validator.min_words,
            validator.max_words,
        )
        == (180, 240)
    )


@pytest.mark.parametrize("allow_corrections", [True, False])
def test_recruiter_language_rule_is_shared_with_generation_review_and_confirmation(
    source_documents, allow_corrections
):
    rule = load_prompt("shared/recruiter_text_rules.j2")
    assert rule in cv_adaptation.build_system_prompt()
    context = build_document_context(**source_documents)
    style = context["recruiter_text_style"]
    assert style["editorial_rules"] == rule
    assert style["source_id"] == "style:recruiter_text"
    assert style["source_id"] in context["source_index"]
    assert style["source_id"] in allowed_source_ids(
        source_documents["profile"], source_documents["adapted_cv"]
    )
    prompts = [
        application_texts.build_user_prompt(**_assembled_inputs(source_documents)),
        document_review.build_user_prompt(
            **source_documents,
            letter=MotivationLetter(subject="Application", body="Interested."),
            validation_issues=[],
            allow_corrections=allow_corrections,
        ),
    ]
    for prompt in prompts:
        start = prompt.rfind("{", 0, prompt.index('"source_index"'))
        exposed, _ = json.JSONDecoder().raw_decode(prompt[start:])
        assert exposed["recruiter_text_style"] == style
