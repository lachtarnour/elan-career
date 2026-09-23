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


@pytest.mark.parametrize(
    ("word_count", "warnings"),
    [
        (179, ["letter_too_short:179"]),
        (180, []),
        (240, []),
        (241, ["letter_too_long:241"]),
    ],
)
def test_default_letter_length_boundaries(word_count: int, warnings: list[str]) -> None:
    profile = get_profile()
    paragraphs = [" ".join(["context"] * count) for count in (60, 60, word_count - 120)]
    result = MotivationLetterValidator(profile).validate(
        MotivationLetter(subject="Application", body="\n\n".join(paragraphs)),
        cv=_cv(profile),
        analysis=_analysis(),
    )

    assert result.ok
    assert result.errors == []
    assert result.warnings == warnings


@pytest.mark.parametrize(
    ("language", "opening"),
    [
        ("en", "I am willing to learn how your team approaches its users' needs."),
        ("en", "I am ready to learn from colleagues working directly with customers."),
        ("fr", "Je suis prêt à apprendre auprès des équipes au contact des usagers."),
        ("fr", "Je suis disposé à apprendre votre manière de travailler."),
    ],
)
def test_willingness_to_learn_is_not_lexically_penalized(language: str, opening: str) -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1).validate(
        _letter(opening),
        cv=_cv(profile),
        analysis=_analysis(language=language),
    )

    assert result.ok
    assert result.warnings == []


def test_rhetorical_assessment_is_left_to_llm_review() -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1).validate(
        _letter("I have limited experience in this field."),
        cv=_cv(profile),
        analysis=_analysis(),
    )

    # Whether the sentence is an unnecessary apology depends on its context;
    # Python only reports objective structure, hygiene and selection findings.
    assert result.errors == []
    assert result.warnings == []


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
    selected_cv = _cv(profile, project_ids=(project.id,))
    unselected_cv = _cv(profile)
    snapshots = [item.model_dump() for item in (profile, selected_cv, unselected_cv, letter)]

    selected_result = validator.validate(letter, cv=selected_cv, analysis=_analysis())
    unselected_result = validator.validate(letter, cv=unselected_cv, analysis=_analysis())

    assert selected_result.warnings == []
    assert f"unselected_project_in_letter:{project.id}" in unselected_result.warnings
    assert [
        item.model_dump() for item in (profile, selected_cv, unselected_cv, letter)
    ] == snapshots


@pytest.mark.parametrize(
    ("language", "body"),
    [
        (
            "en",
            "I want to join your team because the advertised focus on making public "
            "services accessible gives this position a purpose that matters to me.\n\n"
            "Working alongside the people who use the service would help me understand "
            "their needs and contribute to useful, carefully evaluated improvements.\n\n"
            "I would welcome a discussion about the team's priorities and the ways "
            "I could contribute to this mission.",
        ),
        (
            "fr",
            "Je souhaite rejoindre votre équipe car la mission annoncée, rendre les "
            "services publics plus accessibles, donne un sens concret à ce poste.\n\n"
            "La proximité avec les usagers me motive : je souhaite comprendre leurs "
            "besoins et contribuer à des améliorations utiles et évaluées avec soin.\n\n"
            "Je serais heureux de discuter des priorités de votre équipe et de la "
            "manière dont je pourrais participer à cette mission.",
        ),
    ],
)
def test_motivation_only_letter_does_not_require_cv_evidence(language: str, body: str) -> None:
    profile = get_profile()
    result = MotivationLetterValidator(profile, min_words=1).validate(
        MotivationLetter(subject="Application", body=body),
        cv=_cv(profile),
        analysis=_analysis(language=language),
    )

    assert result.ok
    assert result.errors == []
    assert result.warnings == []


def test_unsupported_attribution_requires_semantic_review() -> None:
    profile = get_profile()
    # A company-name or keyword match must not be mistaken for evidence that
    # this precise achievement is true. The independent LLM review handles it.
    letter = _letter(
        f"At {profile.experiences[0].company}, I led a worldwide rollout "
        "of autonomous submarine navigation systems."
    )
    result = MotivationLetterValidator(profile, min_words=1).validate(
        letter, cv=_cv(profile), analysis=_analysis()
    )

    assert result.errors == []
    assert result.warnings == []
