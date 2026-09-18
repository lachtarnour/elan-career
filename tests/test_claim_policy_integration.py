"""Claim-policy changes must preserve static evidence and Skills reconstruction."""

from __future__ import annotations

from pathlib import Path

import pytest

from smartapply.cv import CvAdapter, CvValidator
from smartapply.cv.assembly import profile_experiences
from smartapply.llm import (
    CVPersonalization,
    DocumentTextReview,
    JobAnalysis,
    MockLLMProvider,
    MotivationLetter,
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


def _response(title: str, summary: str) -> CVPersonalization:
    # Deliberately mixes relevant/off-role skills and duplicates. The adapter
    # must still recover offer/summary skills, apply baselines and filter/order.
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
        selected_project_ids=["proj_evidence_rag", "proj_ner_camembert"],
        skills_profile_id="mixed",
        selected_skills=[
            SkillSelectionBlock(category_id=category, skills=skills)
            for category, skills in selected.items()
        ],
        skills_order=list(selected),
        warnings=[],
    )


def _generate(
    profile: Profile,
    analysis: JobAnalysis,
    response: CVPersonalization,
    route: str,
    *,
    letter_body: str = "I developed transcription pipelines using Whisper at Aurore Labs.",
):
    adapter = CvAdapter(
        profile,
        llm=MockLLMProvider(
            {
                "cv_adaptation": response,
                "motivation_letter": MotivationLetter(subject="Application", body=letter_body),
                "document_review": DocumentTextReview(
                    issues=[],
                    professional_summary=None,
                    motivation_letter_subject=None,
                    motivation_letter_body=None,
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
        _response(title, summary),
        route,
    )
    cv, _ = CvValidator(profile).auto_fix(cv)

    assert cv.cv_title == title
    assert cv.professional_summary == summary
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
    response = _response("Speech Engineer", "Developed transcription pipelines using Whisper.")
    response.selected_skills = [
        SkillSelectionBlock(category_id="speech_audio", skills=["Whisper", "Speech-to-text"])
    ]
    response.skills_order = ["speech_audio"]
    letter_text = "I developed transcription pipelines using Whisper at Aurore Labs."

    cv, letter = _generate(
        profile,
        _analysis("Speech Engineer", ["Whisper", "Speech-to-text"], []),
        response,
        "application",
        letter_body=letter_text,
    )
    cleaned, removed = CvValidator(profile).auto_fix(cv)
    selected = {skill for block in cleaned.selected_skills for skill in block.skills}

    assert letter is not None and letter.body == letter_text
    assert "Whisper" not in profile.skills.allowed_skills
    assert "Whisper" not in selected
    assert "Speech-to-text" in selected
    assert selected <= profile.skills.allowed_skills
    assert "selected_skill:Whisper" in removed
    assert cleaned.selected_experiences == profile_experiences(profile)
    assert any("Whisper" in b.text for exp in cleaned.selected_experiences for b in exp.bullets)


# Golden outputs captured from the adapter before removing the claim blacklist.
# Keep these explicit: computing expectations with current contracts would hide
# changes to recovery, role filtering, baseline completion or display ordering.
_SKILLS_GOLDENS = [
    (
        "Data Scientist",
        ["PyTorch", "FAISS"],
        ["RAG"],
        {
            "ml_ai": ["NLP", "PyTorch", "scikit-learn", "TensorFlow"],
            "data_analysis": ["pandas", "Python", "SQL", "R", "NumPy"],
            "data_infra": ["Docker", "Git", "AWS"],
            "generative_agentic_ai": ["FAISS", "RAG"],
        },
    ),
    (
        "Data Engineer",
        ["Spark", "SQL"],
        ["GCP"],
        {
            "data_infra": ["Docker", "Git", "Spark", "CI/CD", "AWS"],
            "data_analysis": ["pandas", "SQL", "Python", "NumPy"],
        },
    ),
    (
        "LLM Engineer",
        ["LangChain", "RAG"],
        ["FAISS"],
        {
            "generative_agentic_ai": ["FAISS", "LangChain", "RAG", "Vector search"],
            "ml_ai": [
                "NLP",
                "Transformers",
                "Fine-tuning",
                "PyTorch",
                "TensorFlow",
                "scikit-learn",
            ],
            "data_infra": ["Docker", "Git", "FastAPI", "AWS"],
            "data_analysis": ["pandas", "Python", "SQL"],
        },
    ),
]


@pytest.mark.parametrize("route", ["cv", "application"])
@pytest.mark.parametrize("role,required,keywords,expected", _SKILLS_GOLDENS)
def test_fixed_response_keeps_pre_change_skills(profile, route, role, required, keywords, expected):
    cv, _ = _generate(
        profile,
        _analysis(role, required, keywords),
        _response(role, "Built reproducible Python workflows."),
        route,
    )
    cv, _ = CvValidator(profile).auto_fix(cv)

    assert {block.category_id: block.skills for block in cv.selected_skills} == expected
    assert [block.category_id for block in cv.selected_skills] == list(expected)
    assert cv.skills_order == list(expected)
    assert cv.selected_experiences == profile_experiences(profile)
