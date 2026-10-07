"""Focused tests for deterministic experience and seniority gates."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from smartapply.filtering.facts import FilterFacts
from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.experience import evaluate_experience
from smartapply.filtering.hard_reject.seniority import evaluate_seniority
from smartapply.filtering.hard_reject.types import GateStatus
from smartapply.filtering.rules import RuleSet


@dataclass
class _Job:
    title: str = "Data Scientist"
    company: str = "Acme"
    description: str = "Build machine-learning models."
    location: str | None = "Paris"
    contract_type: str | None = "CDI"
    remote_policy: str | None = None


def _context(
    *,
    title: str = "Data Scientist",
    description: str = "Build machine-learning models.",
    facts: FilterFacts | None = None,
) -> HardRejectContext:
    job = _Job(title=title, description=description)
    return HardRejectContext.build(
        job=job,
        rules=RuleSet(max_required_years=5),
        facts=facts or FilterFacts(),
    )


@pytest.mark.parametrize(
    "description",
    [
        "Vous devez justifier de 5 ans d'expérience en machine learning.",
        "Expérience demandée: 5 ans d'expérience.",
        "Votre savoir-faire : expérience d'au moins 5 ans en modélisation.",
        "At least 6 years of experience in data science is required.",
        "5+ ans d'expérience en IA.",
    ],
)
def test_mandatory_five_year_minimum_is_blocked(description: str) -> None:
    decision = evaluate_experience(_context(description=description))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes[0].startswith("experience_required_too_high:")
    assert decision.evidence[0].source == "visible_description"
    assert decision.evidence[0].quote


def test_range_uses_its_lower_bound() -> None:
    decision = evaluate_experience(
        _context(description="Vous justifiez entre 3 et 6 ans d'expérience.")
    )

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Expérience de 2 ans minimum à 5 ans en développement web.",
        "Your profile: 3/5 years of experience in pricing analytics.",
    ],
)
def test_ranges_with_repeated_units_or_slash_use_the_lower_bound(
    description: str,
) -> None:
    decision = evaluate_experience(_context(description=description))

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        ("Une expérience de 4 ans minimum en tant qu'Analytics Engineer ou 6 ans en Data Analyst."),
        ("Vous justifiez d'au moins 6 ans en Data Analyst ou de 4 ans en Analytics Engineering."),
        (
            "At least 5 years of data engineering experience, or 3 to 4 years "
            "if the experience is especially dense and technical."
        ),
    ],
)
def test_alternative_experience_paths_use_the_lower_accepted_minimum(
    description: str,
) -> None:
    decision = evaluate_experience(_context(description=description))

    assert decision.status is GateStatus.CLEAR


def test_multiple_seniority_levels_do_not_force_the_highest_band() -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "Plusieurs niveaux de séniorité sont recherchés : de 2 à 5 ans et de 6 à 9 ans."
            )
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_lower_subskill_requirement_does_not_erase_global_minimum() -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "Vous justifiez de 10 ans d'expérience professionnelle au total, "
                "dont 3 ans d'expérience en leadership technique."
            )
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:10+ years",)


def test_structured_and_visible_values_on_same_side_of_threshold_agree() -> None:
    decision = evaluate_experience(
        _context(
            description="Vous justifiez de 5 ans d'expérience en data science.",
            facts=FilterFacts(
                experience_min_years=8,
                experience_required=True,
                experience_requirement="8_YEARS",
                experience_source="ats:experience_level",
            ),
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert not decision.conflicts


def test_required_minimum_wins_over_unrelated_ideal_domain() -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "Minimum 5 ans d'expérience en industrialisation, idéalement dans l'aéronautique."
            )
        )
    )

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "months, expected",
    [(59, GateStatus.CLEAR), (60, GateStatus.BLOCKED)],
)
def test_months_are_compared_without_rounding_up(months: int, expected: GateStatus) -> None:
    decision = evaluate_experience(
        _context(description=f"A minimum of {months} months experience is required.")
    )

    assert decision.status is expected


@pytest.mark.parametrize(
    "description",
    [
        "Idéalement 7 ans d'expérience en data science.",
        "5+ years of experience would be preferred.",
        "7 ans d'expérience sont souhaités.",
        "Une expérience de 5 ans minimum est souhaitée.",
        "Une expérience de 5 ans, minimum, est souhaitée.",
        "Vous disposez idéalement de 5 ans minimum d'expérience.",
        "Au moins 3 ans d'expérience, idéalement plus de 6 ans.",
        "Vous justifiez idéalement d'environ 5 ans d'expérience.",
        "Une préférence pour un profil disposant d'au moins 5 ans d'expérience.",
        "About 5 years of experience in analytics.",
        "Notre entreprise a 20 ans d'expérience dans le logiciel.",
        "We have 20+ years of experience building software.",
        "Our research is supported by 30+ years of experience in AI.",
        "For more than 25 years, Candriam has offered investment solutions.",
        "For more than 170 years, we've worked with our clients.",
        "Earnix builds on more than 25 years of experience in AI.",
        (
            "Avec plus de 100 ans d'expérience dans le secteur de la santé et "
            "environ 47 000 employés dans le monde, la société développe des "
            "technologies médicales."
        ),
        "The plant lifetime extends beyond 40 years and must be evaluated.",
        "Garantir une opération safe et fiable pendant a minima 10 ans.",
        (
            "Master's degree in space engineering (+5 years post-baccalaureate), "
            "with a minimum of 4 years of professional experience."
        ),
        "Le projet, d'une durée de 6 ans, porte sur des modèles prédictifs.",
        "Our 5+ year roadmap includes predictive models.",
        "Prime d'ancienneté au bout de 6 ans.",
        "À 6 mois, vous aurez livré au moins 6 fonctionnalités.",
        "The planned separation will be completed within 18 to 24 months.",
    ],
)
def test_non_mandatory_or_non_candidate_durations_are_clear(description: str) -> None:
    assert evaluate_experience(_context(description=description)).status is GateStatus.CLEAR


def test_structured_and_visible_experience_conflict_is_unresolved() -> None:
    facts = FilterFacts(
        experience_min_years=5,
        experience_required=True,
        experience_requirement="5_TO_10_YEARS",
        experience_source="ats:experience_level",
    )

    decision = evaluate_experience(
        _context(
            description="Vous devez justifier de 3 ans d'expérience.",
            facts=facts,
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("structured=5y;visible=3y",)


@pytest.mark.parametrize(
    "specific_requirement",
    [
        "Vous justifiez idéalement d'une expérience réussie de 4 à 5 ans.",
        "Une expérience confirmée de 5 ans minimum est souhaitée.",
    ],
)
def test_structured_field_echo_does_not_outvote_specific_optional_wording(
    specific_requirement: str,
) -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "Expérience demandée: 5 ans d'expérience.\n\n"
                f"Profil recherché : {specific_requirement}"
            ),
            facts=FilterFacts(
                source="job_board",
                experience_min_years=5,
                experience_required=True,
                experience_requirement="5 ans",
                experience_source="job_board:experience_min",
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("structured_required_but_visible_optional",)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Data Scientist",
        "Staff AI Engineer",
        "Principal Data Scientist",
        "Head of Data Science",
        "Data Science Manager",
        "AI Team Leader",
    ],
)
def test_explicit_senior_or_management_titles_are_blocked(title: str) -> None:
    decision = evaluate_seniority(_context(title=title))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes[0].startswith("seniority_in_title:")
    assert decision.evidence[0].source == "visible_title"


@pytest.mark.parametrize("title", ["Sr Data Scientist", "Lead ML Engineer", "Tech Leader IA"])
def test_grade_only_title_without_firm_requirement_is_unresolved(title: str) -> None:
    decision = evaluate_seniority(_context(title=title))

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("seniority_label_without_firm_requirement",)


@pytest.mark.parametrize("years", [3, 4])
def test_senior_label_with_eligible_minimum_and_mentoring_is_unresolved(years: int) -> None:
    decision = evaluate_seniority(
        _context(
            title="Sr Data Scientist II",
            description=(
                f"As a Senior Data Scientist, you build ML models. {years}+ years of "
                "programming experience. You provide mentoring to other engineers."
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("seniority_label_conflicts_with_eligible_minimum",)


@pytest.mark.parametrize("years", [0, 2, 3])
def test_separate_junior_and_senior_recruitment_paths_are_kept(years: int) -> None:
    decision = evaluate_experience(
        _context(
            description=(
                f"Profil recherché - niveaux\nJUNIOR ({years}-3 ANS)\n"
                "Python et API REST\nSENIOR (6+ ANS)\nExpérience déploiement LLM"
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("multiple_experience_levels_include_eligible_path",)


@pytest.mark.parametrize("structured_years", [0, 1, 3, 4])
def test_low_structured_band_cannot_be_overwritten_by_high_body_minimum(
    structured_years: int,
) -> None:
    decision = evaluate_experience(
        _context(
            description="Vous justifiez de 5 à 8 ans minimum d'expérience en IA.",
            facts=FilterFacts(
                experience_min_years=structured_years,
                experience_required=True,
                experience_source="ats:experience_min",
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == (f"structured={structured_years}y;visible=5y",)


def test_low_experience_banner_and_high_profile_floor_remain_unresolved() -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "CDI | Expérience : 2+ ans | Paris\n\n"
                "Profil recherché : +5 ans d'expérience en data science."
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("visible_experience_header_conflicts_with_profile",)
    assert {item.source for item in decision.evidence} == {"visible_description"}


def test_structured_subskill_floor_does_not_conflict_with_explicit_overall_floor() -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "Expérience demandée: 4 ans d'expérience - 4 à 5 ans ML et 7/8 ans en tout"
            ),
            facts=FilterFacts(
                experience_min_years=4,
                experience_required=True,
                experience_source="ats:experience_min",
            ),
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:7+ years",)


@pytest.mark.parametrize("separator", ["\n", "; ", ", "])
def test_senior_and_manager_experience_paths_keep_the_eligible_path(separator: str) -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "Nous recherchons des profils :\n"
                f"Seniors (3 à 5 ans d'expérience){separator}"
                "Managers (5 à 8 ans d'expérience)"
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("multiple_experience_levels_include_eligible_path",)


def test_all_alternative_experience_paths_above_the_ceiling_still_block() -> None:
    decision = evaluate_experience(
        _context(
            description=(
                "Nous recherchons des profils :\nSeniors (5 à 8 ans d'expérience)\n"
                "Managers (8 à 10 ans d'expérience)"
            ),
        )
    )

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "biography",
    [
        "Our CEO has 20+ years of experience building technology businesses.",
        "Our founder has 15 years of experience in machine learning.",
        "Notre fondateur a 25 ans d'expérience dans les logiciels.",
        "Our CTO has 20+ years of experience building software.",
        "The hiring manager has 10+ years of experience. You need 2 years.",
        "Jane has 20+ years of experience developing AI products.",
        "You will work with Jane who has 20+ years of experience.",
    ],
)
def test_company_leader_biography_does_not_become_candidate_experience(
    biography: str,
) -> None:
    decision = evaluate_experience(_context(description=biography))

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize("separator", [". ", " and ", ", but "])
def test_company_leader_biography_does_not_hide_separate_candidate_requirement(
    separator: str,
) -> None:
    decision = evaluate_experience(
        _context(
            description=f"Our CEO has 20+ years of experience{separator}"
            "you must have 6 years of experience in machine learning.",
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:6+ years",)


@pytest.mark.parametrize(
    "description",
    [
        "The candidate has 6 years of experience in machine learning.",
        "You must have 6 years of experience in machine learning.",
        "You bring 6 years of experience in machine learning.",
        "The successful candidate is required to have 6 years of experience.",
    ],
)
def test_explicit_candidate_possession_remains_a_requirement(description: str) -> None:
    decision = evaluate_experience(_context(description=description))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:6+ years",)


def test_required_experience_owned_by_third_party_is_not_a_candidate_minimum() -> None:
    decision = evaluate_experience(
        _context(
            description="The hiring manager is required to have 10 years of experience. "
            "You need 2 years of experience."
        )
    )

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "5+ years of experience is preferred.",
        "5 years of experience is not required.",
        "You do not need 5 years of experience.",
        "You do not need 5 years of experience. You need 2 years of experience.",
        "Nous n'avons pas besoin de 5 ans d'expérience.",
        "You have up to 5 years of experience.",
        "No more than 5 years of experience is required.",
        "Maximum 5 years of experience required.",
        "5 years maximum of experience required.",
    ],
)
def test_experience_preference_negation_and_upper_bound_do_not_establish_a_floor(
    description: str,
) -> None:
    assert evaluate_experience(_context(description=description)).status is GateStatus.CLEAR


def test_soft_experience_context_does_not_cancel_separate_mandatory_minimum() -> None:
    decision = evaluate_experience(
        _context(
            description="You do not need 5 years in Python. "
            "You must have 6 years of professional experience."
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:6+ years",)


@pytest.mark.parametrize(
    "description",
    [
        "Requirements: 5 years of experience.",
        "Requirements:\nPython and SQL.\n5 years of experience.",
        "Five years of experience required.",
        "At least five years of experience required.",
        "Vous justifiez au moins cinq ans d'expérience.",
        "Cinq ans d'expérience requis.",
        "Au moins cinq ans d'expérience sont requis.",
    ],
)
def test_explicit_required_section_and_written_numbers_remain_blocked(description: str) -> None:
    decision = evaluate_experience(_context(description=description))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:5+ years",)


@pytest.mark.parametrize(
    "description",
    [
        "Requirements:\nFive years of experience is preferred.",
        "Requirements:\nYou do not need five years of experience.",
        "Requirements:\nNo more than five years of experience is required.",
        "Requirements:\nJane has ten years of experience. You need two years.",
        "Nice to have:\nFive years of experience.",
        "Au maximum cinq ans d'expérience requis.",
        "Au moins cinq ans d'expérience seraient appréciés.",
        "Une expérience de cinq ans serait souhaitée.",
        "Five years of experience would be nice to have.",
        "Five years of experience would be a plus.",
        "Five years of experience is a plus.",
        "Cinq ans d'expérience serait un plus.",
        "Cinq ans d'expérience seraient un plus.",
    ],
)
def test_written_numbers_and_requirement_header_do_not_override_soft_or_third_party_context(
    description: str,
) -> None:
    assert evaluate_experience(_context(description=description)).status is GateStatus.CLEAR


def test_mandatory_candidate_floor_wins_over_optional_suffix_in_same_sentence() -> None:
    decision = evaluate_experience(
        _context(description="You must have five years of experience, preferably in data science.")
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:5+ years",)


def test_salary_band_experience_is_not_a_candidate_floor() -> None:
    decision = evaluate_experience(
        _context(
            description="Profil recherché\nVous justifiez idéalement de 2 à 3 ans d'expérience.\n"
            "Conditions\n38 à 42 K€ sur 13 mois, jusqu'à 45 K€ pour un profil de plus de 5 ans d'expérience."
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_salary_band_does_not_erase_independent_mandatory_candidate_experience() -> None:
    decision = evaluate_experience(
        _context(
            description="Salaire : jusqu'à 45 K€ pour un profil de plus de 5 ans d'expérience. "
            "You must have six years of professional experience."
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:6+ years",)


def test_low_minimum_and_high_ideal_band_preserve_eligible_junior_path() -> None:
    decision = evaluate_experience(
        _context(
            description="Expérience\n: minimum 2 ans post étude - idéal 5 ans\n"
            "Profil recherché\n-Idéalement\n5 ans d'expérience en IA.\n-Minimum\n"
            "2 ans d'expérience professionnelle après les études.\n"
            "Nous sommes ouverts à des profils plus juniors."
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_ideal_candidate_subject_does_not_make_firm_requirement_optional() -> None:
    decision = evaluate_experience(
        _context(description="The ideal candidate must have five years of experience.")
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:5+ years",)


@pytest.mark.parametrize(
    ("number", "years"),
    list(
        enumerate(
            [
                "one",
                "two",
                "three",
                "four",
                "five",
                "six",
                "seven",
                "eight",
                "nine",
                "ten",
                "eleven",
                "twelve",
                "thirteen",
                "fourteen",
                "fifteen",
                "sixteen",
                "seventeen",
                "eighteen",
                "nineteen",
                "twenty",
            ],
            start=1,
        )
    ),
)
def test_english_written_minima_one_to_twenty_preserve_the_five_year_threshold(
    number: int, years: str
) -> None:
    decision = evaluate_experience(
        _context(description=f"The candidate must have {years} years of experience.")
    )

    assert decision.status is (GateStatus.BLOCKED if number >= 5 else GateStatus.CLEAR)


@pytest.mark.parametrize(
    ("number", "years"),
    list(
        enumerate(
            [
                "un",
                "deux",
                "trois",
                "quatre",
                "cinq",
                "six",
                "sept",
                "huit",
                "neuf",
                "dix",
                "onze",
                "douze",
                "treize",
                "quatorze",
                "quinze",
                "seize",
                "dix-sept",
                "dix-huit",
                "dix-neuf",
                "vingt",
            ],
            start=1,
        )
    )
    + [(17, "dix sept"), (18, "dix huit"), (19, "dix neuf")],
)
def test_french_written_minima_un_to_vingt_preserve_the_five_year_threshold(
    number: int, years: str
) -> None:
    decision = evaluate_experience(
        _context(description=f"Le candidat doit avoir {years} ans d'expérience.")
    )

    assert decision.status is (GateStatus.BLOCKED if number >= 5 else GateStatus.CLEAR)


def test_required_section_resets_optional_experience_context() -> None:
    decision = evaluate_experience(
        _context(
            description="Nice to have:\nFive years of experience.\nRequirements:\nSix years of experience."
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:6+ years",)


def test_first_experience_profile_and_high_final_banner_remain_unresolved() -> None:
    decision = evaluate_experience(
        _context(
            description="Profil recherché\nTu as une première expérience significative en IA.\n"
            "Informations complémentaires\nExpérience : > 5 ans"
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("entry_profile_conflicts_with_high_experience_banner",)


def test_first_domain_experience_does_not_erase_firm_overall_candidate_minimum() -> None:
    decision = evaluate_experience(
        _context(
            description="You have first experience using ML frameworks. "
            "You must have 6 years of overall professional experience."
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:6+ years",)


def test_requirement_heading_is_not_a_third_party_owner_of_must_have() -> None:
    decision = evaluate_experience(
        _context(
            description="Profil recherché\n— Profil recherché\nMust have\n"
            "4 ans d'expérience dans un environnement exigeant.\nNice to have\nPython.",
            facts=FilterFacts(
                experience_min_years=5,
                experience_required=True,
                experience_source="ats:experience_min",
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("structured=5y;visible=4y",)


@pytest.mark.parametrize("heading", ["Requirements", "Profil recherché"])
def test_subject_omitted_must_have_still_establishes_candidate_requirement(heading: str) -> None:
    decision = evaluate_experience(
        _context(description=f"{heading}\nMust have 6 years of experience.")
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("experience_required_too_high:6+ years",)


def test_responsable_title_without_hierarchy_is_unresolved() -> None:
    decision = evaluate_seniority(_context(title="Responsable de la transformation Data et IA"))

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("responsable_title_without_hierarchy",)


@pytest.mark.parametrize(
    "title",
    ["Future Leaders Programme", "Lead Generation Data Analyst", "Lead Scoring Analyst"],
)
def test_non_seniority_uses_of_lead_are_clear(title: str) -> None:
    assert evaluate_seniority(_context(title=title)).status is GateStatus.CLEAR


def test_non_managerial_responsable_phrase_is_clear() -> None:
    decision = evaluate_seniority(_context(title="Doctorant Data Science et Jeu Responsable"))

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Vous encadrerez une équipe de data scientists.",
        "You will manage a team of machine-learning engineers.",
        "This role has line management responsibilities for the analytics team.",
    ],
)
def test_current_hierarchical_management_is_blocked(description: str) -> None:
    decision = evaluate_seniority(_context(description=description))

    assert decision.status is GateStatus.BLOCKED
    assert decision.evidence[0].source == "visible_description"


@pytest.mark.parametrize(
    "description",
    [
        "Senior data science role focused on causal inference.",
        "Poste senior en data science consacré à l'inférence causale.",
    ],
)
def test_hidden_senior_role_is_blocked(description: str) -> None:
    assert evaluate_seniority(_context(description=description)).status is GateStatus.BLOCKED


def test_junior_confirmed_range_is_not_treated_as_a_senior_only_role() -> None:
    decision = evaluate_seniority(
        _context(
            description=(
                "Nous recrutons un AI & Data Engineer Junior-Confirmé. "
                "Expérience : débutant accepté."
            )
        )
    )

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Vous ne managerez pas d'équipe dans ce poste.",
        "Vous n'aurez pas de responsabilité hiérarchique.",
        "This role has no people management responsibilities.",
        "This is not a senior data science role.",
        "À terme, vous pourrez encadrer une équipe si vous le souhaitez.",
        "You may eventually lead a team as the company grows.",
        (
            "Selon votre potentiel, plusieurs possibilités s'offriront à vous : "
            "pilotage de projet, management d'équipe ou expertise métier."
        ),
    ],
)
def test_negated_or_possible_future_management_is_clear(description: str) -> None:
    assert evaluate_seniority(_context(description=description)).status is GateStatus.CLEAR


def test_future_heading_can_defer_management_without_treating_speed_as_optionality() -> None:
    future = evaluate_seniority(
        _context(description="Évolution rapide :\nManagement d'équipe très vite.")
    )
    mandatory = evaluate_seniority(_context(description="Rapidement, vous managerez une équipe."))

    assert future.status is GateStatus.CLEAR
    assert mandatory.status is GateStatus.BLOCKED


def test_numeric_experience_requirement_is_owned_by_experience_gate() -> None:
    decision = evaluate_experience(
        _context(description="Le poste exige 10+ years of professional experience.")
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.evidence[0].rule_id == "candidate_experience_minimum"
