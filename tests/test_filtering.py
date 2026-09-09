"""Tests for the local filtering module."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from smartapply.filtering import (
    FilterDisposition,
    JobFilter,
    RoleRelevanceDisposition,
    RuleSet,
    assess_role_relevance,
    ruleset_from_preferences,
)
from smartapply.profile import get_profile


@dataclass
class FakeJob:
    title: str
    company: str
    description: str
    location: str | None = None
    contract_type: str | None = None
    remote_policy: str | None = None


def _real_rules() -> RuleSet:
    return ruleset_from_preferences(get_profile().preferences)


def test_target_role_in_paris_kept_high_score() -> None:
    rules = _real_rules()
    f = JobFilter(rules)
    job = FakeJob(
        title="Data Scientist NLP",
        company="Acme",
        description="Build RAG pipelines with PyTorch, Hugging Face. CDI.",
        location="Paris, France",
        contract_type="CDI",
        remote_policy="hybrid",
    )
    res = f.evaluate(job)
    assert res.kept
    assert res.score >= 0.7
    assert "target_role:data scientist" in res.reasons
    assert not any(reason.startswith("location_") for reason in res.reasons)


def test_linkedin_region_location_with_ile_de_france_is_not_foreign() -> None:
    f = JobFilter(_real_rules())
    job = FakeJob(
        title="Data Scientist",
        company="Example Energy",
        description="Build Python ML models for battery monitoring.",
        location="Paris, Île-de-France, France",
        contract_type="Full-time",
        remote_policy=None,
    )

    res = f.evaluate(job)

    assert res.kept
    assert not any("location_rejected_foreign" in reason for reason in res.reasons)


@pytest.mark.parametrize(
    "location",
    [
        "Stuttgart, Germany",
        "Stuttgart, Allemagne",
        "Stuttgart, DE",
        "Madrid, Espagne",
        "Amsterdam, Pays-Bas",
        "London, Royaume-Uni",
    ],
)
def test_selected_worldwide_locations_are_not_statically_rejected(location: str) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob(
            "Data Scientist",
            "Acme",
            "Build machine-learning models with Python.",
            location,
            "CDI",
        )
    )

    assert result.kept
    assert not any("location_rejected" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "description",
    [
        "The position is based in London and focuses on machine learning.",
        "This role will be located in Berlin and focuses on machine learning.",
        "Location: Madrid, Spain. Build machine-learning models with Python.",
        "Le poste est basé à Londres et porte sur le machine learning.",
        "Localisation : Berlin, Allemagne. Développer des modèles avec Python.",
    ],
)
def test_location_text_is_not_rejected_by_the_old_france_gate(description: str) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Paris", "CDI")
    )

    assert result.kept
    assert not any("location_rejected" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "description",
    [
        "Our company is based in London. This position is based in Paris and uses Python.",
        "Notre société est basée à Berlin. Le poste est basé à Paris et utilise Python.",
    ],
)
def test_foreign_company_headquarters_does_not_override_french_job_location(
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Paris", "CDI")
    )

    assert result.kept
    assert not any("location_rejected_foreign" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "description",
    [
        "Vos missions sont de construire des modèles. Profil recherché avec expérience Python.",
        "Responsibilities include building ML models. Requirements include Python experience. Apply now.",
    ],
)
def test_french_and_english_offers_are_accepted(description: str) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Montréal", "CDI")
    )

    assert result.kept
    assert not any("offer_language_not_accepted" in reason for reason in result.reasons)


def test_confidently_detected_unaccepted_language_is_rejected() -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob(
            "Data Scientist",
            "Acme",
            "Wir suchen Verstärkung. Deine Aufgaben umfassen ML. "
            "Anforderungen: Python und Berufserfahrung.",
            "Berlin, Germany",
            "Full-time",
        )
    )

    assert not result.kept
    assert "offer_language_not_accepted:de" in result.reasons


def test_filter_rejects_5plus_years_required() -> None:
    f = JobFilter(_real_rules())
    job = FakeJob(
        title="Data Scientist",
        company="Acme",
        description="Looking for 5+ years of experience in machine learning.",
        location="Paris",
        contract_type="CDI",
    )
    res = f.evaluate(job)
    assert not res.kept
    assert any("experience_required_too_high" in r for r in res.reasons)


def test_filter_rejects_freelance_visible_in_title_without_contract_field() -> None:
    f = JobFilter(_real_rules())
    job = FakeJob(
        title="Analytics Engineer - Freelance H/F",
        company="Acme",
        description="Build dashboards and data models.",
        location="Paris",
        contract_type=None,
    )
    res = f.evaluate(job)
    assert not res.kept
    assert any("blocked_contract_visible_text:freelance" in r for r in res.reasons)


@pytest.mark.parametrize(
    ("description", "reason_prefix"),
    [
        (
            "This is a work-study position focused on Python and ML.",
            "blocked_contract_visible_text",
        ),
        ("This is a co-op position focused on Python and ML.", "blocked_contract_visible_text"),
        (
            "This is a temporary position focused on Python and ML.",
            "blocked_contract_visible_text:cdd",
        ),
        (
            "This is a 6-month contract focused on Python and ML.",
            "blocked_contract_visible_text:cdd",
        ),
        ("This contract position lasts for six months.", "blocked_contract_visible_text"),
        ("This is a self-employed role building ML models.", "blocked_contract_visible_text"),
        ("Freelance mission to build ML models with Python.", "blocked_contract_visible_text"),
        ("Poste temporaire pour développer des modèles ML.", "blocked_contract_visible_text:cdd"),
        ("Contrat temporaire de 6 mois en data science.", "blocked_contract_visible_text:cdd"),
    ],
)
def test_bilingual_visible_incompatible_contracts_are_rejected(
    description: str,
    reason_prefix: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Paris", None)
    )

    assert not result.kept
    assert any(reason.startswith(reason_prefix) for reason in result.reasons)


@pytest.mark.parametrize(
    "description",
    [
        "Previous internship experience is accepted. Build ML models with Python.",
        "Prior apprenticeship experience is preferred. Build ML models with Python.",
        "You will mentor interns while building ML models with Python.",
        "Une première expérience en stage est acceptée. Développer des modèles avec Python.",
        "Vous encadrerez un alternant et développerez des modèles avec Python.",
    ],
)
def test_bilingual_previous_training_experience_is_not_a_contract_false_positive(
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Paris", "CDI")
    )

    assert result.kept
    assert not any("blocked_contract" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "description",
    [
        "This is a part-time position building ML models with Python.",
        "Employment type: part time. Build ML models with Python.",
        "Ce poste est à temps partiel et porte sur des modèles ML avec Python.",
        "Temps de travail : mi-temps. Développer des modèles ML avec Python.",
    ],
)
def test_bilingual_part_time_visible_text_is_rejected(description: str) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Paris", None)
    )

    assert not result.kept
    assert "blocked_work_time_visible_text:part_time" in result.reasons


def test_filter_keeps_power_bi_when_analytical_ownership_is_explicit() -> None:
    f = JobFilter(_real_rules())
    job = FakeJob(
        title="Data Analyst BI",
        company="Acme",
        description="Power BI dashboards with SQL, Python, statistical analysis and forecasting.",
        location="Paris",
        contract_type="CDI",
    )
    res = f.evaluate(job)
    assert res.kept
    assert not any(reason.startswith("description_hard_reject:") for reason in res.reasons)


@pytest.mark.parametrize("technology", ["Snowflake", "Databricks", "Terraform"])
def test_infrastructure_technology_mention_alone_is_not_a_hard_reject(
    technology: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob(
            "Data Scientist",
            "Acme",
            f"Develop machine-learning models with Python; {technology} is used by the platform.",
            "Paris",
            "CDI",
        )
    )

    assert result.kept
    assert not any(reason.startswith("description_hard_reject:") for reason in result.reasons)


@pytest.mark.parametrize(
    ("title", "description", "expected_concepts"),
    [
        (
            "R&D Engineer",
            "You will design and evaluate autonomous AI agents for statistical learning with Python.",
            {"agentic_ai", "machine_learning"},
        ),
        (
            "Ingénieur R&D",
            "Vous serez chargé de concevoir des agents IA autonomes et des modèles "
            "d'apprentissage statistique avec Python.",
            {"agentic_ai", "machine_learning"},
        ),
        (
            "Ingénieur R&D",
            "Design and deploy des systèmes multi-agents pour l'apprentissage automatique.",
            {"agentic_ai", "machine_learning"},
        ),
        (
            "Agentic AI Developer",
            "Build reliable planning and evaluation workflows.",
            {"agentic_ai"},
        ),
        (
            "Ingénieur en apprentissage statistique",
            "Concevoir des modèles robustes pour la prévision.",
            {"machine_learning", "forecasting"},
        ),
        (
            "Ingénieur R&D",
            "Concevoir et évaluer des modèles bayésiens pour la détection d'anomalies.",
            {"statistical_modeling"},
        ),
    ],
)
def test_bilingual_emerging_ai_and_statistical_terms_are_relevant(
    title: str,
    description: str,
    expected_concepts: set[str],
) -> None:
    rules = _real_rules()
    assessment = assess_role_relevance(
        title=title,
        description=description,
        positive_title_keywords=rules.positive_title_keywords,
        target_roles=rules.target_roles,
    )

    assert assessment.disposition is RoleRelevanceDisposition.RELEVANT
    assert expected_concepts.issubset(set(assessment.concepts))


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Account Executive — Agentic AI",
            "Sell our autonomous-agent platform to enterprise customers.",
        ),
        (
            "Commercial solutions IA",
            "Commercialiser une solution d'IA agentique auprès de grands comptes.",
        ),
        (
            "Juriste conformité IA",
            "Assurer la conformité AI Act et la réglementation de l'IA.",
        ),
        (
            "Data Center Operations Engineer",
            "Maintain critical facilities and electrical infrastructure.",
        ),
        (
            "Operations Coordinator",
            "Our company is a market leader in agentic AI. Coordinate vendor schedules.",
        ),
    ],
)
def test_bilingual_non_technical_ai_and_literal_data_contexts_are_rejected(
    title: str,
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(FakeJob(title, "Acme", description, "Paris", "CDI"))

    assert not result.kept
    assert result.disposition is FilterDisposition.REJECTED


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Web Analytics Specialist",
            "Own GA4, GTM, the data layer and the tracking plan.",
        ),
        (
            "Spécialiste webanalyse",
            "Définir le plan de marquage, la mesure d'audience et la gestion des tags.",
        ),
    ],
)
def test_bilingual_tracking_only_roles_are_rejected(
    title: str,
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(FakeJob(title, "Acme", description, "Paris", "CDI"))

    assert not result.kept
    assert "web_analytics_tracking_focus" in result.reasons


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Product Data Analyst",
            "Define the tracking plan, run A/B tests and perform segmentation with SQL.",
        ),
        (
            "Analyste produit data",
            "Définir le plan de marquage, conduire des tests A/B et réaliser des "
            "segmentations avec SQL.",
        ),
    ],
)
def test_bilingual_product_analytics_with_ownership_is_kept(
    title: str,
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(FakeJob(title, "Acme", description, "Paris", "CDI"))

    assert result.kept


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Reporting Analyst",
            "Produce recurring dashboards and management reporting with Tableau.",
        ),
        (
            "Analyste reporting",
            "Produire des tableaux de bord récurrents pour le contrôle de gestion.",
        ),
    ],
)
def test_bilingual_reporting_without_analytical_ownership_is_rejected(
    title: str,
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(FakeJob(title, "Acme", description, "Paris", "CDI"))

    assert not result.kept
    assert "reporting_bi_without_analytical_ownership" in result.reasons


@pytest.mark.parametrize(
    "description",
    [
        "Reporting and dashboards without Python or analytical ownership.",
        "Reporting et tableaux de bord, sans Python ni responsabilité analytique.",
        "Business intelligence dashboards without SQL.",
        "Tableaux de bord décisionnels sans SQL.",
    ],
)
def test_bilingual_reporting_with_explicitly_missing_data_tech_is_rejected(
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Reporting Analyst", "Acme", description, "Paris", "CDI")
    )

    assert not result.kept
    assert "reporting_without_core_data_tech" in result.reasons


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Data Engineer",
            "Build Airflow ETL pipelines for the enterprise data warehouse.",
        ),
        (
            "Ingénieur Data",
            "Concevoir des pipelines Airflow et ETL pour un entrepôt de données.",
        ),
    ],
)
def test_bilingual_pure_data_engineering_is_rejected(
    title: str,
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(FakeJob(title, "Acme", description, "Paris", "CDI"))

    assert not result.kept
    assert "pure_data_engineering_role" in result.reasons


def test_data_engineering_with_statistical_learning_scope_is_kept() -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob(
            "Data Engineer",
            "Acme",
            "Build Airflow feature pipelines for statistical learning and ML models.",
            "Paris",
            "CDI",
        )
    )

    assert result.kept
    assert "pure_data_engineering_role" not in result.reasons


@pytest.mark.parametrize(
    "description",
    [
        "Build Airflow pipelines serving deep learning and computer vision models.",
        "Build Airflow pipelines for generative AI, LLM and RAG systems.",
        "Concevoir des pipelines Airflow pour l'apprentissage profond et la vision par ordinateur.",
        "Concevoir des pipelines ETL pour l'IA générative et le traitement du langage naturel.",
    ],
)
def test_bilingual_data_engineering_serving_ml_scope_is_kept(
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Engineer", "Acme", description, "Paris", "CDI")
    )

    assert result.kept
    assert "pure_data_engineering_role" not in result.reasons


@pytest.mark.parametrize(
    "description",
    [
        "You manage a data team and define its machine-learning roadmap.",
        "You will supervise a team of data scientists.",
        "The role includes line management responsibilities for the analytics team.",
        "Vous encadrerez une équipe de data scientists.",
        "Vous managerez une équipe et définirez la feuille de route ML.",
    ],
)
def test_bilingual_candidate_leadership_responsibility_is_rejected(
    description: str,
) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Paris", "CDI")
    )

    assert not result.kept
    assert "seniority_or_leadership_in_description" in result.reasons


@pytest.mark.parametrize(
    "description",
    [
        "Senior data science role focused on causal inference.",
        "Poste senior en data science consacré à l'inférence causale.",
    ],
)
def test_bilingual_hidden_senior_role_is_rejected(description: str) -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob("Data Scientist", "Acme", description, "Paris", "CDI")
    )

    assert not result.kept
    assert "seniority_or_leadership_in_description" in result.reasons


def test_senior_colleague_mention_is_not_a_candidate_seniority_false_positive() -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob(
            "Data Scientist",
            "Acme",
            "Collaborate with senior data scientists and build Python ML models.",
            "Paris",
            "CDI",
        )
    )

    assert result.kept
    assert "seniority_or_leadership_in_description" not in result.reasons


def test_unknown_vocabulary_is_kept_as_uncertain_for_semantic_ranking() -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob(
            "Ingénieur en systèmes décisionnels avancés",
            "Acme",
            "Concevoir des architectures neurosymboliques pour l'aide à la décision.",
            "Paris",
            "CDI",
        )
    )

    assert result.kept
    assert result.disposition is FilterDisposition.UNCERTAIN
    assert "role_relevance:uncertain_kept_for_semantic_ranking" in result.reasons


def test_incidental_statistical_knowledge_is_not_treated_as_a_data_mission() -> None:
    rules = _real_rules()
    assessment = assess_role_relevance(
        title="Strategy Consultant",
        description="Experience with statistical models would be a plus.",
        positive_title_keywords=rules.positive_title_keywords,
        target_roles=rules.target_roles,
    )

    assert assessment.disposition is RoleRelevanceDisposition.UNCERTAIN
    assert "candidate_mission" not in assessment.evidence


def test_business_data_analyst_is_not_rejected_for_the_word_business() -> None:
    result = JobFilter(_real_rules()).evaluate(
        FakeJob(
            "Business Data Analyst",
            "Acme",
            "Analyze product data with Python, SQL and controlled experiments.",
            "Paris",
            "CDI",
        )
    )

    assert result.kept
    assert not any(reason == "title_hard_reject:business" for reason in result.reasons)


def test_filter_rejects_clear_off_target_it_or_adjacent_titles() -> None:
    f = JobFilter(_real_rules())
    titles = (
        "Ingénieure Pédagogique Digital Learning",
        "Ingénieur Automatisme et Digital Twin",
        "Chargé d'études techniques actuarielles",
        "Ingénieur Logiciels Embarqués H/F",
        "Consultant.e - Cybersécurité",
        "Consultant en Agilité / IT operating model",
        "Administrateur Applications & Systèmes SI (BI/API/SQL)",
    )
    for title in titles:
        res = f.evaluate(
            FakeJob(
                title=title,
                company="Acme",
                description="Python, IA, données, reporting et projets digitaux.",
                location="Paris",
                contract_type="CDI",
            )
        )
        assert not res.kept, title


def test_filter_rejects_cdd_even_when_fulltime_is_visible() -> None:
    f = JobFilter(_real_rules())
    job = FakeJob(
        title="Data Scientist",
        company="Acme",
        description="Build pipelines with Python.",
        location="Paris",
        contract_type="CDD temps plein",
    )
    res = f.evaluate(job)

    assert not res.kept
    assert any("blocked_contract_type" in reason for reason in res.reasons)


def test_filter_many_partitions_jobs() -> None:
    f = JobFilter(_real_rules())
    jobs = [
        FakeJob("Data Scientist", "Acme", "PyTorch", "Paris", "CDI"),
        FakeJob("Sales Director", "Beta", "Quotas", "Paris", "CDI"),
        FakeJob("ML Engineer", "Gamma", "MLOps with AWS", "Lyon", "CDI"),
    ]
    kept, evaluated = f.filter_many(jobs)
    assert len(evaluated) == 3
    assert len(kept) == 2
    assert "Sales Director" not in {j.title for j in kept}
