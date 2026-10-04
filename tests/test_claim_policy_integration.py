"""Claim-policy changes must preserve static evidence and Skills reconstruction."""

from __future__ import annotations

from pathlib import Path

import pytest

from smartapply.cv import CvAdapter, CvValidator
from smartapply.cv.assembly import profile_experiences
from smartapply.llm import (
    ApplicationTexts,
    CVPersonalization,
    CVSelection,
    DocumentTextReview,
    JobAnalysis,
    MockLLMProvider,
    SkillSelectionBlock,
)
from smartapply.profile import Profile, load_profile
from smartapply.ranking import MockEmbeddingsProvider


@pytest.fixture
def profile() -> Profile:
    return load_profile(Path(__file__).parent / "fixtures" / "profile")


def _analysis(role: str, required: list[str], keywords: list[str]) -> JobAnalysis:
    return JobAnalysis(
        role_type=role,
        seniority="mid",
        domain="Technology",
        main_tasks=[],
        required_skills=required,
        nice_to_have=[],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=keywords,
    )


def _response(title: str, summary: str, offer_evidence: str) -> CVPersonalization:
    # Deliberately mixes role-specific skills and duplicates. The adapter
    # must retain catalogue-backed selections, recover offer skills, add
    # baselines and display each skill only once per category.
    selected = {
        "data_infra": ["Docker", "Git", "Git"],
        "ml_ai": ["NLP"],
        "generative_agentic_ai": ["FAISS"],
        "data_analysis": ["pandas"],
        "stats_signal": ["Markov chains"],
    }
    return CVPersonalization(
        cv_title=title,
        professional_summary=summary,
        selected_project_ids=["proj_evidence_rag", "proj_ner_camembert", "proj_smartapply"],
        project_selection_reasons=[
            {
                "project_id": project_id,
                "reason": "Relevant documented evidence for the requested technical work.",
                "offer_evidence": offer_evidence,
                "complementary_value": "",
            }
            for project_id in ["proj_evidence_rag", "proj_ner_camembert", "proj_smartapply"]
        ],
        skills_profile_id="mixed",
        selected_skills=[
            SkillSelectionBlock(category_id=category, skills=skills)
            for category, skills in selected.items()
        ],
        skills_order=list(selected),
        warnings=[],
    )


_LETTER_BODY = (
    "I am interested in work that connects careful data preparation with useful applications. "
    "The opportunity to discuss technical choices with people who will use the results "
    "is especially appealing to me. I would like to contribute in a setting where teams "
    "consider the available evidence, evaluate their assumptions and explain the limits "
    "of a solution before deciding how to improve it.\n\n"
    "I developed transcription pipelines using Whisper at Aurore Labs. "
    "That work is one reason I want to continue exploring the relationship between "
    "data processing and practical needs. I enjoy considering how each stage of a "
    "pipeline affects the next and how the resulting information can be checked. "
    "I would welcome the chance to deepen that approach through collaboration with "
    "colleagues bringing different perspectives to the same problem.\n\n"
    "I would be glad to contribute to clear implementation and evaluation practices, "
    "while learning about the particular needs of your team. My interest is in "
    "building useful solutions through deliberate technical choices and open discussion "
    "of results. I would welcome a conversation about the role and the ways my "
    "experience could support the work you are planning."
)


def _generate(
    profile: Profile,
    analysis: JobAnalysis,
    response: CVPersonalization,
    route: str,
    *,
    letter_body: str = _LETTER_BODY,
):
    adapter = CvAdapter(
        profile,
        llm=MockLLMProvider(
            {
                "cv_adaptation": CVSelection.model_validate(
                    {
                        **response.model_dump(exclude={"professional_summary"}),
                        "selected_certificate_ids": response.selected_certificate_ids or [],
                    }
                ),
                "application_texts": ApplicationTexts(
                    professional_summary=response.professional_summary,
                    motivation_letter_subject="Application",
                    motivation_letter_body=letter_body,
                ),
                "document_review": DocumentTextReview(
                    issues=[],
                ),
            }
        ),
        embeddings=MockEmbeddingsProvider(),
    )
    if route == "cv":
        cv, _ = adapter.adapt(analysis, job_title=analysis.role_type, job_company="Example")
        return cv, None
    cv, letter, _ = adapter.adapt_application(
        analysis, job_title=analysis.role_type, job_company="Example", language="en"
    )
    return cv, letter


@pytest.mark.parametrize("route", ["cv", "application"])
def test_evidence_backed_headline_is_not_rewritten_by_vendor_blacklist(profile, route):
    # A synthetic verified experience proves these claims; they deliberately
    # remain absent from the display catalogue, which is not a claim whitelist.
    title = "Data Engineer"
    summary = "Built Reporting datasets on GCP with Python and SQL."
    profile.experiences[0].title = title
    bullet = profile.experiences[0].bullets[0]
    bullet.text = summary
    bullet.allowed_claims = [summary]
    bullet.evidence_level = "verified"
    before = profile.model_dump()
    assert "GCP" not in profile.skills.allowed_skills

    cv, _ = _generate(
        profile,
        _analysis(title, ["GCP", "SQL"], ["Reporting"]),
        _response(title, summary, "SQL"),
        route,
    )
    cv, _ = CvValidator(profile).auto_fix(cv)

    assert cv.cv_title == title
    if route == "application":
        assert cv.professional_summary == summary
    else:
        assert "professional_summary" not in cv.model_dump()
    assert cv.selected_experiences == profile_experiences(profile)
    assert profile.model_dump() == before
    assert not any(w.startswith("unsupported_cv_head_terms_replaced:") for w in cv.warnings)


def test_documented_letter_tool_does_not_open_the_display_catalogue(profile):
    # Reproduce the editorial choice: Whisper remains in experience evidence,
    # while Speech-to-text is the name the candidate wants in the Skills section.
    speech = next(
        category for category in profile.skills.categories if category.id == "speech_audio"
    )
    speech.skills = [skill for skill in speech.skills if skill != "Whisper"]
    speech.skills.append("Speech-to-text")
    response = _response(
        "Speech Engineer", "Developed transcription pipelines using Whisper.", "Whisper"
    )
    response.selected_skills = [
        SkillSelectionBlock(category_id="speech_audio", skills=["Whisper", "Speech-to-text"])
    ]
    response.skills_order = ["speech_audio"]
    letter_text = _LETTER_BODY

    cv, letter = _generate(
        profile,
        _analysis("Speech Engineer", ["Whisper", "Speech-to-text"], []),
        response,
        "application",
        letter_body=letter_text,
    )
    assert "Whisper" not in {
        skill for block in cv.selected_skills for skill in block.skills
    }
    cleaned, removed = CvValidator(profile).auto_fix(cv)
    selected = {skill for block in cleaned.selected_skills for skill in block.skills}

    assert letter is not None and letter.body == letter_text
    assert "Whisper" not in profile.skills.allowed_skills
    assert "Whisper" not in selected
    assert "Speech-to-text" in selected
    assert selected <= profile.skills.allowed_skills
    assert removed == []
    assert cleaned.selected_experiences == profile_experiences(profile)
    assert any("Whisper" in b.text for exp in cleaned.selected_experiences for b in exp.bullets)


# Keep the Skills contract explicit: offer recovery, catalogue normalization,
# baseline completion and display ordering happen before the summary is generated.
# In the LLM case, Python was formerly inserted only because the summary named
# it; that dependency is intentionally removed, preserving the selected content.
# Preserved categories reach Data Engineer's minimum skill count without NumPy;
# the one-skill category polish still appends a second supported skill per block.
# Automatic choices and their display suffix use the configured category priority.
_SKILLS_GOLDENS = [
    (
        "Data Scientist",
        ["PyTorch", "FAISS"],
        ["RAG"],
        {
            "data_infra": ["Docker", "Git", "AWS"],
            "ml_ai": ["NLP", "PyTorch", "scikit-learn", "TensorFlow"],
            "generative_agentic_ai": ["FAISS", "RAG"],
            "data_analysis": ["pandas", "Python", "SQL", "NumPy", "R"],
            "stats_signal": ["Markov chains", "Monte Carlo methods"],
        },
    ),
    (
        "Data Engineer",
        ["Spark", "SQL"],
        ["GCP"],
        {
            "data_infra": ["Docker", "Git", "CI/CD", "AWS", "Spark"],
            "ml_ai": ["NLP", "Anomaly detection"],
            "generative_agentic_ai": ["FAISS", "LangChain"],
            "data_analysis": ["pandas", "Python", "SQL"],
            "stats_signal": ["Markov chains", "Monte Carlo methods"],
        },
    ),
    (
        "LLM Engineer",
        ["LangChain", "RAG"],
        ["FAISS"],
        {
            "data_infra": ["Docker", "Git", "FastAPI", "AWS"],
            "ml_ai": [
                "NLP",
                "PyTorch",
                "scikit-learn",
                "TensorFlow",
                "Transformers",
            ],
            "generative_agentic_ai": ["FAISS", "LangChain", "RAG", "Vector search"],
            "data_analysis": ["pandas", "SQL"],
            "stats_signal": ["Markov chains", "Monte Carlo methods"],
        },
    ),
]


@pytest.mark.parametrize("route", ["cv", "application"])
@pytest.mark.parametrize("role,required,keywords,expected", _SKILLS_GOLDENS)
def test_fixed_selection_skills_do_not_depend_on_summary(
    profile, route, role, required, keywords, expected
):
    cv, _ = _generate(
        profile,
        _analysis(role, required, keywords),
        _response(role, "Built reproducible Python workflows.", required[0]),
        route,
    )
    cv, _ = CvValidator(profile).auto_fix(cv)

    assert {block.category_id: block.skills for block in cv.selected_skills} == expected
    assert [block.category_id for block in cv.selected_skills] == list(expected)
    assert cv.skills_order == list(expected)
    assert cv.selected_experiences == profile_experiences(profile)
    if role == "LLM Engineer" and route == "application":
        assert "Python" in cv.professional_summary
        assert "Python" not in {skill for block in cv.selected_skills for skill in block.skills}


@pytest.mark.parametrize("route", ["cv", "application"])
def test_explicit_llm_category_order_survives_offer_recovery_and_generation(profile, route):
    role, required, keywords, expected = _SKILLS_GOLDENS[0]
    response = _response(role, "Built reproducible Python workflows.", required[0])
    response.skills_order = [
        "stats_signal",
        "generative_agentic_ai",
        "data_infra",
        "data_analysis",
        "ml_ai",
    ]

    cv, _ = _generate(profile, _analysis(role, required, keywords), response, route)
    cv, _ = CvValidator(profile).auto_fix(cv)

    assert {block.category_id: block.skills for block in cv.selected_skills} == expected
    assert [block.category_id for block in cv.selected_skills] == response.skills_order
    assert cv.skills_order == response.skills_order
