"""Focused tests for role, language, and qualification hard gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from smartapply.filtering.hard_reject.context import (
    CandidateQualifications,
    HardRejectContext,
)
from smartapply.filtering.hard_reject.language import evaluate_language
from smartapply.filtering.hard_reject.qualification import evaluate_qualification
from smartapply.filtering.hard_reject.role import evaluate_role
from smartapply.filtering.hard_reject.role_families import (
    TITLE_FAMILIES,
    TitleFamilyPolicy,
    match_title_families,
)
from smartapply.filtering.hard_reject.seniority import evaluate_seniority
from smartapply.filtering.hard_reject.types import GateDecision, GateStatus
from smartapply.filtering.rules import RuleSet
from smartapply.filtering.source_facts import FilterFacts


@dataclass
class FakeJob:
    title: str
    company: str
    description: str
    location: str | None = "Paris"
    contract_type: str | None = "CDI"
    remote_policy: str | None = None
    id: int = 1
    source: str = "test"
    source_data: dict[str, Any] | None = None
    application_url: str | None = None


_FAMILY_EXAMPLES = {
    "sales_commercial": "Sales Representative",
    "business_analysis_operations": "Business Analyst",
    "building_hvac": "Technicien CVC",
    "industrial_methods_maintenance": "Ingenieur maintenance industrielle",
    "control_automation": "Ingenieur controle commande",
    "hardware_electronics": "Hardware Engineer",
    "mechanical_civil_materials": "Ingenieur mecanique",
    "infrastructure_operations": "Infrastructure Engineer",
    "quality_validation_safety": "Quality Assurance Engineer",
    "data_platform": "Data Engineer",
    "reporting_bi": "Reporting Analyst",
    "web_analytics_tracking": "Web Analytics Specialist",
    "project_product_management": "Product Owner",
    "laboratory_domain_science": "Clinical Scientist",
    "legal_compliance": "Juriste RGPD",
    "cybersecurity_identity": "Cybersecurity Engineer",
    "academic_teaching": "Teacher",
    "training_ambiguous": "AI Trainer",
    "industrial_studies_operations": "Operateur de fabrication",
    "geospatial_surveying": "Specialiste geomatique",
    "people_administration": "Talent Acquisition Recruiter",
    "people_analytics": "People Analytics",
    "marketing_customer": "Customer Success Manager",
    "marketing_analytics": "Marketing Analyst",
    "support_and_administration": "Database Administrator",
    "data_literal_not_analytics": "Data Center Engineer",
    "general_software_development": "Backend Developer",
    "actuarial_analytics": "Analyste actuarielle",
}


def _context(
    title: str,
    description: str,
    *,
    rules: RuleSet | None = None,
    has_doctorate: bool | None = None,
    company: str = "Acme",
    job_id: int = 1,
    source: str = "test",
) -> HardRejectContext:
    return HardRejectContext.build(
        job=FakeJob(
            id=job_id,
            title=title,
            company=company,
            description=description,
            source=source,
        ),
        rules=rules or RuleSet(deal_breakers=["sales"]),
        facts=FilterFacts(),
        qualifications=CandidateQualifications(has_doctorate=has_doctorate),
    )


def _assert_auditable_block(
    decision: GateDecision,
    *,
    source_text: str,
) -> None:
    assert decision.status is GateStatus.BLOCKED
    assert decision.evidence
    for item in decision.evidence:
        assert item.source == "visible_text"
        assert item.quote
        assert item.quote in source_text


def test_every_title_family_has_an_explicit_policy_example() -> None:
    declared = {family.name: family.policy for family in TITLE_FAMILIES}
    assert set(_FAMILY_EXAMPLES) == set(declared)

    for family_name, title in _FAMILY_EXAMPLES.items():
        matches = {match.name: match.policy for match in match_title_families(title.lower())}
        assert matches[family_name] is declared[family_name]
        decision = evaluate_role(_context(title, "Assurer les missions principales du poste."))
        expected_status = (
            GateStatus.BLOCKED
            if declared[family_name] is TitleFamilyPolicy.HARD_REJECT
            else GateStatus.UNRESOLVED
        )
        assert decision.status is expected_status, family_name


def test_target_role_veto_applies_to_every_hard_title_family() -> None:
    for family in TITLE_FAMILIES:
        if family.policy is not TitleFamilyPolicy.HARD_REJECT:
            continue
        title = f"{_FAMILY_EXAMPLES[family.name]} / Data Scientist"
        decision = evaluate_role(_context(title, "Contribuer aux missions du poste."))
        assert decision.status is not GateStatus.BLOCKED, family.name


def test_deferred_neighbour_does_not_erase_a_decisive_hard_family() -> None:
    for family in TITLE_FAMILIES:
        if family.policy is not TitleFamilyPolicy.HARD_REJECT:
            continue
        title = f"{_FAMILY_EXAMPLES[family.name]} / Data Engineer"
        decision = evaluate_role(_context(title, "Assurer les missions du poste."))
        assert decision.status is GateStatus.BLOCKED, family.name


@pytest.mark.parametrize(
    "title",
    [
        "Sales Representative",
        "Technicien CVC",
        "Juriste RGPD",
        "Talent Acquisition Recruiter",
        "Comptable",
        "Enseignant en mathématiques",
        "Ingénieur mécanique",
        "Customer Success Manager",
    ],
)
def test_explicitly_incompatible_occupations_remain_hard_rejected(title: str) -> None:
    description = "Piloter les activités opérationnelles du poste."

    decision = evaluate_role(_context(title, description))

    _assert_auditable_block(decision, source_text=f"{title}\n{description}")
    assert decision.reason_codes


@pytest.mark.parametrize(
    "title",
    [
        "Data Engineer",
        "Software Engineer Backend",
        "Cloud Infrastructure Engineer",
        "Ingénieur DevOps",
        "Cybersecurity Engineer",
        "Ingénieur QA Automatisation",
        "Product Owner",
        "Business Analyst",
        "Sales Operations & GTM Engineer",
        "Reporting Analyst",
        "Web Analytics Specialist",
        "Ingénieur électronique",
        "Ingénieur Contrôle Commande",
        "Solutions Architect",
        "Expert Automation",
    ],
)
def test_technical_or_analytical_neighbours_are_deferred_to_relevance(
    title: str,
) -> None:
    decision = evaluate_role(_context(title, "Piloter les activités techniques du poste."))

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    assert decision.conflicts
    assert all(item.startswith("role_family_requires_relevance:") for item in decision.conflicts)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "AI & Automation Engineer - Sales Focus",
            "You will design and deploy RAG systems and LLM agents.",
        ),
        (
            "Juriste / Data Scientist",
            "Vous concevez des évaluations de modèles de langage.",
        ),
        (
            "AI Trainers Network",
            "You will train AI models by reviewing and correcting their answers.",
        ),
    ],
)
def test_mixed_title_or_candidate_ai_mission_cannot_be_hard_rejected_by_role(
    title: str,
    description: str,
) -> None:
    decision = evaluate_role(_context(title, description))

    assert decision.status is not GateStatus.BLOCKED
    assert not decision.reason_codes
    assert decision.evidence
    assert decision.notes


@pytest.mark.parametrize(
    "title",
    [
        "AI Trainer",
        "LLM Trainer",
        "Model Trainer",
        "Trainer",
        "Formateur IA",
        "Formatrice de modèles IA",
        "Formateur",
    ],
)
def test_ambiguous_trainer_titles_are_deferred_to_relevance(title: str) -> None:
    decision = evaluate_role(_context(title, "Contribuer aux missions de l'équipe."))

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    assert "role_family_requires_relevance:training_ambiguous" in decision.conflicts


@pytest.mark.parametrize(
    "title",
    [
        "Team Lead - Lead Generation Data Analyst",
    ],
)
def test_non_senior_lead_phrase_does_not_hide_a_real_lead_title(title: str) -> None:
    decision = evaluate_seniority(_context(title, "Construire des modèles."))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("seniority_in_title:lead",)


def test_technical_lead_title_keeps_ambiguous_grade_without_people_management() -> None:
    decision = evaluate_seniority(
        _context("Lead Data Scientist / Lead Scoring", "Construire des modèles.")
    )

    assert decision.status is GateStatus.UNRESOLVED


@pytest.mark.parametrize("years", [3, 4])
def test_confirmed_recruitment_label_is_not_a_hierarchy_requirement(years: int) -> None:
    decision = evaluate_seniority(
        _context(
            "Data Scientist Confirmé",
            "Nous recherchons un Data Scientist confirmé. "
            f"Minimum {years} ans d'expérience. Construire des modèles ML.",
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_confirmed_label_with_actual_supervision_still_blocks() -> None:
    decision = evaluate_seniority(
        _context(
            "Data Scientist Confirmé",
            "Vos missions\nConstruire des modèles ML\nEncadrer un(e) data scientist",
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.evidence[0].quote == "encadrer un(e) data scientist"


@pytest.mark.parametrize(
    "description",
    [
        "Encadrer des data scientists juniors et participer aux revues de code.",
        "Encadrer des ingénieurs juniors pour intégrer les résultats de simulation.",
        "Encadrer des ingénieur*es junior et des étudiant*es en master.",
    ],
)
def test_junior_technical_mentoring_alone_does_not_prove_line_management(
    description: str,
) -> None:
    decision = evaluate_seniority(_context("Data Scientist", description))

    assert decision.status is not GateStatus.BLOCKED


@pytest.mark.parametrize(
    "description",
    [
        "Encadrement( encadrement d'une équipe ) Non",
        "Responsabilité hiérarchique : Non",
        "Line management responsibilities: No",
        "You have no line management responsibilities.",
    ],
)
def test_explicit_negative_hierarchy_field_never_establishes_management(
    description: str,
) -> None:
    decision = evaluate_seniority(_context("Data Scientist", description))

    assert decision.status is not GateStatus.BLOCKED


@pytest.mark.parametrize(
    "description",
    [
        "Nice to have: people management experience.",
        "People management experience is preferred.",
        "Nice to have\nExperience with clients\nPeople management experience.",
        "Management d'équipe serait un plus.",
    ],
)
def test_optional_people_management_does_not_establish_hierarchical_role(
    description: str,
) -> None:
    decision = evaluate_seniority(_context("Data Scientist", description))

    assert decision.status is not GateStatus.BLOCKED


def test_required_responsibilities_reset_optional_management_section() -> None:
    decision = evaluate_seniority(
        _context(
            "Data Scientist",
            "Nice to have\nPeople management experience.\nResponsibilities\n"
            "You will manage a data team.",
        )
    )

    assert decision.status is GateStatus.BLOCKED


def test_negative_hierarchy_field_does_not_erase_separate_required_management() -> None:
    decision = evaluate_seniority(
        _context(
            "Data Scientist",
            "Responsabilité hiérarchique: Non\nYou will manage a data team as part of this role.",
        )
    )

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    ("title", "conflict"),
    [
        ("Data Scientist Junior / Senior", "multiple_seniority_levels_in_title"),
        ("ML Engineer - multiple levels", "multiple_seniority_levels_in_title"),
        ("Responsable Data/IA", "responsable_title_without_hierarchy"),
    ],
)
def test_ambiguous_seniority_titles_are_deferred(
    title: str,
    conflict: str,
) -> None:
    decision = evaluate_seniority(_context(title, "Construire des modèles."))

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == (conflict,)


@pytest.mark.parametrize(
    "title",
    ["Senior Data Scientist", "Responsable d'une équipe Data"],
)
def test_explicit_hierarchical_seniority_title_still_blocks(title: str) -> None:
    decision = evaluate_seniority(_context(title, "Construire des modèles."))

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "title",
    ["Responsable Data", "Data Scientist Junior / Senior"],
)
def test_description_hierarchy_overrides_ambiguous_seniority_title(title: str) -> None:
    decision = evaluate_seniority(_context(title, "Vous managerez une équipe de data scientists."))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("seniority_or_leadership_in_description",)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Sales Representative",
            "Our company builds LLM agents. You prepare commercial proposals.",
        ),
        (
            "Recruiter",
            "You use Microsoft Copilot to write candidate messages.",
        ),
    ],
)
def test_company_ai_context_or_tool_use_does_not_erase_a_decisive_occupation(
    title: str,
    description: str,
) -> None:
    decision = evaluate_role(_context(title, description))

    _assert_auditable_block(decision, source_text=f"{title}\n{description}")
    assert "role_title_veto:candidate_ai_mission" not in decision.notes


@pytest.mark.parametrize(
    "title",
    [
        "AI Architect",
        "Chef de projet Data Analyst",
        "Product Manager (Research Engineer)",
        "Ingénieur QA Automatisation Python & IA",
        "INGENIEUR EN IA SPECIALISE EN CYBERSECURITE",
        "Ingénieur logiciel vision",
        "Ingénieur-e IA/DevOps",
    ],
)
def test_mixed_target_titles_are_never_decided_by_the_hard_role_gate(
    title: str,
) -> None:
    decision = evaluate_role(_context(title, "Piloter la conception de solutions techniques."))

    assert decision.status is not GateStatus.BLOCKED
    assert not decision.reason_codes


def test_adjacent_role_family_is_deferred_without_a_rejection_keyword_list() -> None:
    decision = evaluate_role(
        _context(
            "Backend Architect",
            "Concevoir la plateforme technique.",
            rules=RuleSet(deal_breakers=[]),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes


def test_user_role_deal_breaker_remains_explicit_and_other_gates_keep_ownership() -> None:
    rules = RuleSet(deal_breakers=["sales", "stage", "lead 10+ years"])

    sales = evaluate_role(_context("Sales Representative", "Prospecter des comptes.", rules=rules))
    internship = evaluate_role(
        _context("Stage Data Scientist", "Construire un modèle ML.", rules=rules)
    )

    assert sales.status is GateStatus.BLOCKED
    assert internship.status is GateStatus.CLEAR


def test_words_embedded_in_unrelated_titles_do_not_trigger_role_family() -> None:
    architecture = evaluate_role(
        _context(
            "Thèse sur les architectures d'apprentissage social",
            "Étudier des méthodes de décision.",
        )
    )
    video = evaluate_role(
        _context(
            "Ingénieur R&D vidéosurveillance",
            "Développer des méthodes de vision.",
        )
    )

    assert architecture.status is GateStatus.CLEAR
    assert video.status is GateStatus.CLEAR


def test_reporting_without_technology_is_deferred_to_relevance() -> None:
    decision = evaluate_role(
        _context(
            "Reporting Analyst",
            "Reporting only without Python or SQL.",
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes


def test_role_decision_is_invariant_to_identity_company_source_and_punctuation() -> None:
    variants = [
        _context(
            "Sales Representative",
            "Prospecter des comptes.",
            company="First Company",
            job_id=1,
            source="board-a",
        ),
        _context(
            "Sales / Representative",
            "Prospecter des comptes.",
            company="Other Company",
            job_id=999_999,
            source="board-b",
        ),
    ]

    decisions = [evaluate_role(context) for context in variants]

    assert {decision.status for decision in decisions} == {GateStatus.BLOCKED}
    assert all(decision.evidence for decision in decisions)


def test_product_analytics_with_analytical_ownership_is_not_hard_rejected() -> None:
    decision = evaluate_role(
        _context(
            "Product Data Analyst",
            "Define the tracking plan, run A/B tests and perform segmentation with SQL.",
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_language_gate_rejects_only_confidently_detected_unaccepted_language() -> None:
    rules = RuleSet(accepted_job_languages=["fr", "en"])
    decision = evaluate_language(
        _context(
            "Data Scientist",
            "Wir suchen Verstärkung. Deine Aufgaben umfassen ML. "
            "Anforderungen: Python und Berufserfahrung.",
            rules=rules,
        )
    )

    _assert_auditable_block(
        decision,
        source_text=(
            "Data Scientist\nWir suchen Verstärkung. Deine Aufgaben umfassen ML. "
            "Anforderungen: Python und Berufserfahrung."
        ),
    )
    assert "offer_language_not_accepted:de" in decision.reason_codes


def test_language_gate_fails_open_when_detection_is_not_confident() -> None:
    decision = evaluate_language(_context("Data Scientist", "Python, ML, Paris."))

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("A PhD is preferred but not required. You will build ML models.", GateStatus.CLEAR),
        ("PhD required or equivalent professional experience.", GateStatus.UNRESOLVED),
        (
            "You hold a PhD in Computer Science or have equivalent experience.",
            GateStatus.UNRESOLVED,
        ),
        (
            "PhD or an equivalent record of applied research and engineering impact.",
            GateStatus.CLEAR,
        ),
        (
            "Our company collaborates with doctoral researchers. You will build ML models.",
            GateStatus.CLEAR,
        ),
    ],
)
def test_non_mandatory_doctorate_does_not_block(description: str, expected: GateStatus) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is expected


def test_mandatory_doctorate_blocks_only_known_non_doctorate_candidate() -> None:
    decision = evaluate_qualification(
        _context(
            "Research Engineer",
            "A Ph.D. is mandatory for this position.",
            has_doctorate=False,
        )
    )

    _assert_auditable_block(
        decision,
        source_text="Research Engineer\nA Ph.D. is mandatory for this position.",
    )
    assert decision.reason_codes == ("qualification_required:doctorate",)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Artificial Intelligence Researcher",
            "You've got what it takes if you hold a PhD in Computer Science.",
        ),
        ("Postdoctoral Researcher in AI", "Build scientific models."),
        (
            "Ingénieur IA",
            "Vous êtes titulaire d'un diplôme de niveau thèse/PhD en physique.",
        ),
        ("Clinical Scientist", "Expected profile\nPhD in Physics or Engineering."),
    ],
)
def test_explicit_possession_or_postdoctoral_role_requires_completed_doctorate(
    title: str,
    description: str,
) -> None:
    decision = evaluate_qualification(_context(title, description, has_doctorate=False))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("qualification_required:doctorate",)


def test_doctoral_candidate_title_does_not_require_completed_doctorate() -> None:
    decision = evaluate_qualification(
        _context(
            "Doctorant en apprentissage profond",
            "La thèse porte sur les réseaux de neurones.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_doctorate_alternative_to_engineering_degree_is_not_mandatory() -> None:
    decision = evaluate_qualification(
        _context(
            "Ingénieur chercheur",
            "Vous êtes diplômé d'une école d'ingénieur ou titulaire d'un doctorat.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Titulaire d'un BAC+5 (ingénieur, master 2, doctorat, etc.).",
        "Vous êtes titulaire d'un diplôme bac+5 ou doctorat en informatique.",
        "Master requis (Doctorat/PhD de préférence).",
        "PhD in mathematics OR Master's Degree in a quantitative field.",
        "Education: You hold a master's or PhD degree in immunology.",
        "PhD in machine learning, or an MSc with research experience.",
        (
            "One of the following qualifications:\n"
            "MSc in Engineering or Applied Mathematics\n"
            "Engineering degree with a quantitative background\n"
            "PhD in Applied Mathematics"
        ),
        (
            "Nous recherchons un candidat présentant l'un des deux profils suivants :\n"
            "Profil ingénieur diplômé d'une école d'ingénieur\n"
            "Profil docteur titulaire d'un doctorat en data science"
        ),
    ],
)
def test_doctorate_in_degree_alternative_is_not_mandatory(description: str) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.CLEAR


def test_mandatory_doctorate_is_unresolved_when_candidate_fact_is_unknown() -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", "Doctorat obligatoire.", has_doctorate=None)
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("candidate_doctorate_unknown",)


def test_mandatory_doctorate_is_clear_when_candidate_has_one() -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", "PhD required.", has_doctorate=True)
    )

    assert decision.status is GateStatus.CLEAR
    assert decision.notes == ("qualification_satisfied:doctorate",)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Computer Vision Engineer",
            "Nice to Have\nPhD in Computer Science or Computer Vision.",
        ),
        (
            "AI Critical Systems Certification Engineer",
            "Formation académique : Doctorat en IA ; Ingénieur aéronautique ; "
            "Licence ou Master en apprentissage machine embarqué.",
        ),
        (
            "Ingénieur de recherche / post-doctorant",
            "Niveau de diplôme et formations : Doctorat, ingénieur de recherche.",
        ),
        (
            "AI Research Engineer",
            "You should apply if you hold an MSc or PhD in Robotics.\n"
            "Nice to have\nPhD in Robotics with top-tier publications.",
        ),
        (
            "Ingénieur d'études en bio-informatique",
            "Niveau master 2 souhaité.\nDes offres d'emplois recommandées pour vous\n"
            "Doctorat en mathématiques et informatique fondamentale (H/F)",
        ),
    ],
)
def test_doctorate_false_contexts_do_not_block(
    title: str,
    description: str,
) -> None:
    decision = evaluate_qualification(_context(title, description, has_doctorate=False))

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Nice to have\nPublications in top venues\nPhD required in Computer Science.",
        "Atouts souhaités\nPublications scientifiques\nDoctorat obligatoire en IA.",
    ],
)
def test_optional_qualification_section_applies_to_all_its_bullets(
    description: str,
) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.CLEAR


def test_required_section_resets_optional_qualification_context() -> None:
    decision = evaluate_qualification(
        _context(
            "Research Engineer",
            "Nice to have\nPublications in top venues\n"
            "Requirements\nPhD required in Computer Science.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("qualification_required:doctorate",)


@pytest.mark.parametrize("heading", ["Bonus point:", "Bonus points:", "Bonus:"])
def test_doctorate_bonus_section_does_not_create_a_mandatory_degree(heading: str) -> None:
    decision = evaluate_qualification(
        _context(
            "Data Scientist Engineer",
            "You hold a Master's-level degree or above (MSc, engineering school, PhD).\n"
            f"{heading}\nYou have cybersecurity experience.\n"
            "You are comfortable with Spark.\nYou hold a PhD in computer science.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Formations recherchées\nDoctorat/Ingénieur grandes écoles",
        "Formation recommandée : Doctorat",
        "Expected qualification: PhD or engineering degree.",
    ],
)
def test_postdoctoral_label_does_not_override_explicit_degree_alternative(
    description: str,
) -> None:
    decision = evaluate_qualification(
        _context("Post-doctorat en recherche", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.CLEAR


def test_mandatory_doctorate_after_bonus_section_still_blocks() -> None:
    decision = evaluate_qualification(
        _context(
            "Research Scientist",
            "Bonus point:\nYou have publications.\n"
            "Required qualifications:\nYou hold a PhD in computer science.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "description",
    [
        "Experience mentoring PhD students is required.",
        "You will supervise PhD students when required.",
        "You must support PhD candidates in their research.",
    ],
)
def test_doctorate_of_supervised_students_is_not_a_candidate_degree(
    description: str,
) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("doctorate_requirement_owner_unresolved",)


def test_student_degree_mention_does_not_erase_separate_mandatory_candidate_degree() -> None:
    decision = evaluate_qualification(
        _context(
            "Research Engineer",
            "You must hold a PhD to supervise PhD students.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "description",
    [
        "This role cannot operate without Python and SQL.",
        "We can't perform these analyses without Python and SQL.",
        "Ce rôle ne peut pas fonctionner sans Python et SQL.",
    ],
)
def test_negated_dependency_is_not_an_explicit_absence_of_data_technology(
    description: str,
) -> None:
    decision = evaluate_role(_context("Reporting Analyst", description))

    assert decision.status is not GateStatus.BLOCKED


def test_reporting_absence_clause_is_also_deferred_to_relevance() -> None:
    decision = evaluate_role(
        _context(
            "Reporting Analyst",
            "Our research cannot operate without Python. "
            "This reporting role works without Python and SQL.",
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description", ["0 direct reports.", "People management experience would be a plus."]
)
def test_zero_reports_and_optional_management_do_not_block(description: str) -> None:
    decision = evaluate_seniority(_context("Data Scientist", description))

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description", ["1 direct report.", "People management experience is required."]
)
def test_nonzero_reports_and_mandatory_management_still_block(description: str) -> None:
    decision = evaluate_seniority(_context("Data Scientist", description))

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "description",
    [
        "Placé sous la responsabilité hiérarchique du chef de division, vous analysez les données.",
        "You work under the line management responsibilities of your supervisor.",
        "Chaque collaborateur est accompagné par un manager technique, responsable d'une équipe à taille humaine.",
        "You will be supported by a technical manager, responsible for leading a team.",
        "Your supervisor, responsible for managing a team, will support your work.",
        "Vous serez accompagné par un manager technique, responsable d'une équipe. "
        "À terme, vous pourrez évoluer vers le management d'équipe.",
    ],
)
def test_reporting_to_or_support_by_a_manager_does_not_make_candidate_a_manager(
    description: str,
) -> None:
    decision = evaluate_seniority(_context("Data Scientist", description))

    assert decision.status is GateStatus.CLEAR


def test_third_party_management_does_not_erase_candidate_management_in_same_clause() -> None:
    decision = evaluate_seniority(
        _context(
            "Data Scientist",
            "You work with a supervisor, responsible for managing a team, "
            "and you will manage a data team as part of your role.",
        )
    )

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "description",
    ["No PhD is required.", "No doctorate is required.", "Aucun doctorat n'est requis."],
)
def test_explicitly_negated_doctorate_does_not_block(description: str) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Our client scientist is required to hold a PhD.",
        "An external PI must hold a PhD.",
        "The consultant has the required PhD.",
        "The project must have a PhD holder as sponsor.",
        "Required PhD-level review is supplied by an external expert.",
        "Experience mentoring PhD students is required.",
        "Qualifications:\nThe client scientist is required to hold a PhD.",
    ],
)
def test_doctorate_not_attributed_to_candidate_remains_unresolved(description: str) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("doctorate_requirement_owner_unresolved",)


@pytest.mark.parametrize(
    "description",
    [
        "You must hold a PhD in computer science.",
        "The successful candidate is required to hold a PhD.",
        "A PhD is required.",
        "Doctorat obligatoire en informatique.",
        "Candidate requirements: PhD required.",
        "Candidate/profile requirements: PhD required.",
        "Profile requirements:\nPhD required in mathematics.",
        "You have to mentor doctoral students.\nRequirements:\nPhD in computer science.",
    ],
)
def test_doctorate_explicitly_owned_by_candidate_remains_mandatory(description: str) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("qualification_required:doctorate",)


@pytest.mark.parametrize("degree", ["MS", "MSc", "Master", "Master's degree"])
def test_doctorate_alternative_to_master_alias_is_not_mandatory(degree: str) -> None:
    decision = evaluate_qualification(
        _context(
            "Research Engineer",
            f"You hold a PhD or {degree} in Computer Science.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_doctorate_is_one_path_in_bachelor_master_doctorate_requirement_list() -> None:
    decision = evaluate_qualification(
        _context(
            "Research Engineer",
            "Minimum Qualifications\nBachelor's degree in Computer Science and 4+ years of experience.\nOR\n"
            "Master's degree in Computer Science and 3+ years of experience.\nOR\n"
            "PhD in Computer Science and 2+ years of experience.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.CLEAR


@pytest.mark.parametrize(
    "description",
    [
        "Vous êtes titulaire d'un doctorat ou justifiez d'au moins trois ans d'expérience en recherche.",
        "Vous êtes titulaire du DQPRM ou d'un doctorat en physique médicale.",
        "Vous êtes titulaire d'un doctorat en matériaux ou d'une formation scientifique de niveau équivalent.",
        "You must have a professional research qualification or hold a PhD.",
    ],
)
def test_unassessed_non_doctoral_path_cannot_prove_qualification_exclusion(
    description: str,
) -> None:
    decision = evaluate_qualification(
        _context("Research Engineer", description, has_doctorate=False)
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert decision.conflicts == ("qualification_alternatives_unresolved",)


def test_alternative_doctoral_fields_still_require_a_doctorate() -> None:
    decision = evaluate_qualification(
        _context(
            "Research Engineer",
            "You must hold a PhD in Physics or Computer Science.",
            has_doctorate=False,
        )
    )

    assert decision.status is GateStatus.BLOCKED
