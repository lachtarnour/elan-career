"""Letters use profile evidence, not the display catalogue as a truth whitelist."""

from __future__ import annotations

import pytest

from smartapply.cv.assembly import profile_experiences
from smartapply.cv.motivation_validator import MotivationLetterValidator
from smartapply.llm import AdaptedCV, JobAnalysis, MotivationLetter
from smartapply.profile import Profile, get_profile


def _analysis(*, language: str = "en", terms: tuple[str, ...] = ()) -> JobAnalysis:
    return JobAnalysis(
        role_type="Data Scientist",
        seniority="mid",
        domain="Technology",
        main_tasks=[],
        required_skills=list(terms),
        nice_to_have=[],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=list(terms),
        offer_language=language,
    )


def _cv(profile: Profile, *, project_ids: tuple[str, ...] = ()) -> AdaptedCV:
    return AdaptedCV(
        cv_title="Data Scientist",
        professional_summary="Profile-grounded summary.",
        selected_experiences=profile_experiences(profile),
        selected_project_ids=list(project_ids),
        selected_skills=[],
        skills_order=[],
        warnings=[],
    )


def _letter(first_paragraph: str) -> MotivationLetter:
    return MotivationLetter(
        subject="Application",
        body=f"{first_paragraph}\n\nI value clear communication.\n\nThank you for your time.",
    )


@pytest.mark.parametrize("tool", ["Whisper", "Kubernetes"])
def test_documented_tool_outside_display_catalogue_is_not_lexically_blocked(tool: str) -> None:
    profile = get_profile().model_copy(deep=True)
    for category in profile.skills.categories:
        category.skills = [skill for skill in category.skills if skill != tool]
    source = profile.experiences[0]
    source.bullets[0].text = f"Developed reliable processing pipelines using {tool}."
    source.bullets[0].allowed_claims = [source.bullets[0].text]
    source.bullets[0].keywords = []
    assert tool not in profile.skills.allowed_skills
    letter = _letter(f"At {source.company}, I developed processing pipelines using {tool}.")

    result = MotivationLetterValidator(profile, min_words=1).validate(
        letter, cv=_cv(profile), analysis=_analysis(terms=(tool,))
    )

    assert result.errors == []
    assert result.warnings == []


def test_offer_keywords_do_not_change_validation_of_identical_letter() -> None:
    profile = get_profile()
    letter = _letter("At Aurore Labs, I developed speech transcription pipelines using Whisper.")
    validator = MotivationLetterValidator(profile, min_words=1)

    baseline = validator.validate(letter, cv=_cv(profile), analysis=_analysis())
    with_offer_terms = validator.validate(
        letter,
        cv=_cv(profile),
        analysis=_analysis(terms=("Whisper", "speech transcription", "pipelines")),
    )

    assert with_offer_terms == baseline


def test_foreign_script_remains_an_error() -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1).validate(
        _letter("At Aurore Labs, I developed pipelines. Привет."),
        cv=_cv(profile),
        analysis=_analysis(),
    )

    assert not result.ok
    assert "foreign_script_in_letter:cyrillic" in result.errors


def test_short_length_and_paragraph_guards_remain() -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile).validate(
        MotivationLetter(subject="Application", body="Aurore Labs."),
        cv=_cv(profile),
        analysis=_analysis(),
    )

    assert "letter_too_short:2" in result.warnings
    assert "letter_not_3_paragraphs:1" in result.warnings


def test_maximum_length_guard_remains() -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1, max_words=10).validate(
        _letter("At Aurore Labs, I developed reliable speech transcription pipelines."),
        cv=_cv(profile),
        analysis=_analysis(),
    )

    assert any(warning.startswith("letter_too_long:") for warning in result.warnings)


def test_self_deprecation_guard_remains() -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1).validate(
        _letter("At Aurore Labs, I have limited experience in this field."),
        cv=_cv(profile),
        analysis=_analysis(),
    )

    assert "letter_self_deprecation:limited_knowledge_en" in result.warnings


def test_french_elision_guard_remains() -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1).validate(
        _letter("Chez Aurore Labs, j ai développé des pipelines."),
        cv=_cv(profile),
        analysis=_analysis(language="fr"),
    )

    assert "french_elision_missing_apostrophe" in result.warnings


def test_unselected_project_guard_remains() -> None:
    profile = get_profile()
    project = profile.projects[0]
    letter = _letter(f"My work on {project.name} involved evaluating retrieval quality.")
    validator = MotivationLetterValidator(profile, min_words=1)

    selected_result = validator.validate(
        letter, cv=_cv(profile, project_ids=(project.id,)), analysis=_analysis()
    )
    unselected_result = validator.validate(letter, cv=_cv(profile), analysis=_analysis())

    assert selected_result.warnings == []
    assert f"unselected_project_in_letter:{project.id}" in unselected_result.warnings


def test_selected_evidence_reference_guard_remains() -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1).validate(
        _letter("I look forward to discussing this position with your team."),
        cv=_cv(profile),
        analysis=_analysis(),
    )

    assert "letter_may_not_reference_selected_evidence" in result.warnings
