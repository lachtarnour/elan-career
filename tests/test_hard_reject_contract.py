"""Focused tests for the deterministic contract gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.contract import evaluate_contract
from smartapply.filtering.hard_reject.types import GateStatus
from smartapply.filtering.rules import RuleSet
from smartapply.filtering.source_facts import FilterFacts


@dataclass
class FakeJob:
    title: str = "Data Scientist"
    company: str = "Acme"
    description: str = "Build machine-learning models with Python."
    location: str | None = "Paris"
    contract_type: str | None = "CDI"
    remote_policy: str | None = None
    application_url: str | None = None
    source_data: dict[str, Any] | None = None


def _context(
    *,
    facts: FilterFacts | None = None,
    accepted_contract_types: list[str] | None = None,
    **job_values: Any,
) -> HardRejectContext:
    job = FakeJob(**job_values)
    return HardRejectContext.build(
        job=job,
        rules=RuleSet(
            accepted_contract_types=(
                ["CDI"]
                if accepted_contract_types is None
                else accepted_contract_types
            )
        ),
        facts=facts or FilterFacts(),
    )


@pytest.mark.parametrize(
    ("contract_type", "expected_tag"),
    [
        ("Stage", "internship"),
        ("Alternance", "apprenticeship"),
        ("Freelance", "freelance"),
        ("CDD temps plein", "fixed_term"),
    ],
)
def test_explicit_incompatible_contract_metadata_remains_blocked(
    contract_type: str,
    expected_tag: str,
) -> None:
    decision = evaluate_contract(_context(contract_type=contract_type))

    assert decision.status is GateStatus.BLOCKED
    assert any(expected_tag in reason for reason in decision.reason_codes)
    assert decision.evidence
    assert decision.evidence[0].source == "job.contract_type"
    assert decision.evidence[0].quote == contract_type


@pytest.mark.parametrize(
    ("description", "expected_reason"),
    [
        (
            "Offre de stage conventionné pour développer des modèles ML.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "Poste en alternance pour développer des modèles ML.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Mission en freelance pour développer des modèles ML.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "Poste en CDD de 12 mois pour développer des modèles ML.",
            "blocked_contract_visible_text:cdd",
        ),
        (
            "This is a part-time position building ML models.",
            "blocked_work_time_visible_text:part_time",
        ),
        (
            "Part-time position available.",
            "blocked_work_time_visible_text:part_time",
        ),
    ],
)
def test_explicit_visible_contracts_remain_blocked_with_evidence(
    description: str,
    expected_reason: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.BLOCKED
    assert expected_reason in decision.reason_codes
    assert decision.evidence
    assert all(item.source and item.quote for item in decision.evidence)


@pytest.mark.parametrize(
    ("description", "expected_reason"),
    [
        (
            "Our team is hiring for an internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "We are recruiting for an apprenticeship.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "We are looking for a contractor.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "We are hiring a freelancer.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "Notre équipe recrute pour un stage.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "Nous recherchons une personne en alternance.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Nous recrutons un freelance.",
            "blocked_contract_visible_text:freelance",
        ),
    ],
)
def test_explicit_candidate_recruitment_contract_is_blocked(
    description: str,
    expected_reason: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (expected_reason,)


@pytest.mark.parametrize(
    ("description", "expected_reason"),
    [
        (
            "We offer a six-month internship in Paris.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "Nous proposons un stage de 6 mois.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "Join our team as an intern.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "Vous rejoindrez notre équipe en tant que stagiaire.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "This opportunity is an apprenticeship.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Ce rôle est en alternance.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Freelance opportunity to build ML models.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "You will work as a contractor.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "Vous interviendrez en freelance.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "An internship is offered for this position.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "Un stage est proposé pour ce poste.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "An apprenticeship is offered.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Une alternance est proposée.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Un CDD est proposé.",
            "blocked_contract_visible_text:cdd",
        ),
        (
            "This role comes as an internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "The position comes as an apprenticeship.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Internship available.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "Apprenticeship position available.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Freelance role available immediately.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "Stage 6 mois.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "Internship 6 months.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "Apprenticeship 12 months.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "CDD 12 mois.",
            "blocked_contract_visible_text:cdd",
        ),
        (
            "Freelance - Paris.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "Internship | Paris.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "Stage rémunéré de 6 mois.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "Paid internship, 6 months.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "As an intern, you will build machine learning models.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "As a contractor, you will deploy models.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "As an apprentice you will join the team.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "En tant que stagiaire, vous développerez des modèles.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "En tant que freelance, vous interviendrez sur le projet.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "We will hire you as an intern.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "We hire you as an apprentice.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "You are hired as a contractor.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "Vous serez recruté comme stagiaire.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "Nous vous recrutons en alternance.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Nous vous embauchons comme freelance.",
            "blocked_contract_visible_text:freelance",
        ),
    ],
)
def test_compositional_candidate_contract_relation_is_blocked(
    description: str,
    expected_reason: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (expected_reason,)


@pytest.mark.parametrize(
    "description",
    [
        "Our team manages an internship program for local students.",
        "You will be responsible for the internship program and its mentors.",
        "Join our team to manage the internship program.",
        "We offer support to our internship program.",
        "Previous contractor experience is preferred.",
        "Internship experience is welcome for this permanent role.",
        "Freelance work is an optional part of your previous background.",
        "Our careers page lists permanent, fixed-term and internship opportunities.",
        "You will work with a contractor on model deployment.",
        "The role applies apprentissage automatique to forecasting.",
        "The role includes managing interns and the internship program.",
        "The internship program supports local students.",
        "This role is an internship program manager.",
        "We are hiring an internship program manager.",
        "This position is an apprenticeship program coordinator.",
    ],
)
def test_compositional_contract_context_rejects_adversarial_false_signals(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "title",
    [
        "Internship Program Manager",
        "Apprenticeship Program Coordinator",
    ],
)
def test_program_responsibility_title_is_not_a_candidate_contract(
    title: str,
) -> None:
    decision = evaluate_contract(
        _context(
            title=title,
            description="Build and operate data products.",
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "facts",
    [
        FilterFacts(
            source="serpapi",
            structured_contract_type="Internship",
            structured_contract_source="serpapi:schedule_type",
        ),
        FilterFacts(
            source="francetravail",
            structured_contract_type="Apprenticeship",
            structured_contract_source="francetravail:typeContrat",
        ),
        FilterFacts(
            source="welcometothejungle",
            structured_contract_type="Freelance",
            structured_contract_source="welcometothejungle:contract_type",
        ),
        FilterFacts(
            source="francetravail",
            structured_contract_type="CDD",
            structured_contract_source="francetravail:typeContrat",
        ),
        FilterFacts(
            source="francetravail",
            structured_work_time="Temps partiel",
            structured_work_time_source="francetravail:dureeTravail",
        ),
    ],
)
def test_incompatible_structured_facts_remain_blocked(facts: FilterFacts) -> None:
    decision = evaluate_contract(
        _context(facts=facts, contract_type=None)
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.evidence
    assert all(item.source and item.quote for item in decision.evidence)


def test_structured_alternance_boolean_remains_blocked() -> None:
    decision = evaluate_contract(
        _context(
            facts=FilterFacts(
                source="francetravail",
                structured_alternance=True,
            ),
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (
        "blocked_contract_structured:alternance (tag 'apprenticeship')",
    )
    assert decision.evidence[0].field == "structured_alternance"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Data Scientist",
            "Une première expérience en stage est acceptée. Développer des modèles ML.",
        ),
        (
            "Data Scientist",
            "Previous internship experience is preferred. Build ML models.",
        ),
        (
            "Data Scientist",
            "Vous encadrerez un alternant et développerez des modèles ML.",
        ),
        (
            "Data Scientist",
            "Notre cabinet est un leader du travail temporaire. Le poste est en CDI.",
        ),
        (
            "First Stage Propulsion Engineer",
            "Design propulsion control models with Python.",
        ),
        (
            "Ingénieur en apprentissage automatique",
            "Concevoir des modèles statistiques avec Python.",
        ),
        (
            "Thèse CIFRE - Apprentissage par renforcement décentralisé",
            "Le poste porte sur des modèles de décision séquentielle.",
        ),
        (
            "AI Engineer",
            "Excellent junior profiles with significant GenAI internship experience "
            "may also be considered.",
        ),
        (
            "Data Scientist",
            "Every day you will receive apprenticeship, coaching and training.",
        ),
        (
            "Data Scientist",
            "Our learning and apprenticeship culture helps you grow.",
        ),
        (
            "AI Research Assistant",
            "Research experience demonstrated via an internship or publications.",
        ),
        (
            "AI Engineer",
            "A significant project (internship, research, or personal portfolio) is valued.",
        ),
        (
            "AI Software Engineer",
            "The team includes a Tech Lead à mi-temps who will be your manager.",
        ),
        (
            "Data Scientist",
            "Notre cabinet de recrutement est spécialisé depuis 40 ans dans le "
            "recrutement en CDI, en intérim et en management de transition.",
        ),
        (
            "Applied AI Engineer",
            "Vous avez déjà conçu et livré un produit auprès d'utilisateurs réels "
            "(startup, projet personnel, mission indépendante).",
        ),
        (
            "Data Scientist",
            "Avec plus de 90 agences d'intérim, ARTUS vous accompagne dans vos "
            "recherches de mission d'intérim ou d'emploi en CDD ou CDI.",
        ),
    ],
)
def test_non_contract_context_does_not_create_a_false_rejection(
    title: str,
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(title=title, description=description, contract_type="CDI")
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "Apprentissage en continu : certifications et formations en libre accès.",
        "Un environnement favorisant le partage et l'apprentissage continu.",
        "Apprentissage à fond : certifications, ateliers et labs inclus.",
        "Architectures de réseaux neuronaux et méthodes d'apprentissage.",
        "Licence ou Master en Apprentissage Machine Embarqué & Données.",
        "Notre école forme les équipes en articulant travail et apprentissage.",
        "Expérience de travail ou stage en tant qu'ingénieur logiciel.",
        "Deux ans d'expérience sont requis, expérience en alternance acceptée.",
        (
            "Une première expérience en instrumentation est souhaitée "
            "(stage de fin d'études de 6 mois, alternance ou premier emploi)."
        ),
        "Une première expérience idéalement acquise via un stage ou une alternance.",
        "Une expérience ou un stage en agroécologie sera apprécié.",
        (
            "Les parcours incluant une alternance ou des expériences "
            "significatives en stage sont étudiés avec attention."
        ),
        (
            "Cette expérience peut avoir été acquise dans le cadre d’une "
            "alternance."
        ),
        (
            "Une première expérience en bureau d’études, acquise en CDI, "
            "stage ou alternance, est appréciée."
        ),
        (
            "Une première expérience réussie (CDI, CDD ou alternance) est "
            "demandée."
        ),
        "Prior experience (internship or apprenticeship) is valued.",
        (
            "Assurer l’astreinte administrative téléphonique en alternance "
            "avec l’adjointe de direction et le directeur."
        ),
        "We are open to conversations about part-time hours.",
        (
            "Notre cabinet propose des offres d'emploi dans l'industrie, "
            "en CDI / CDD ou intérim."
        ),
        (
            "Notre groupe se spécialise dans le recrutement en CDI, CDD "
            "et travail temporaire."
        ),
        "Un recrutement CDD-CDI de précision accompagne les candidats.",
        (
            "Notre agence vous accompagne dans votre recherche d'emploi "
            "(intérim, CDD, CDI)."
        ),
        "Notre société est un acteur du recrutement en CDI, CDD et intérim.",
        "Cette société d'ingénierie indépendante intervient dans toute l'Europe.",
        "Filiale indépendante d'un groupe, la société conçoit des logiciels.",
        (
            "The First Stage team develops First Stage software and validates "
            "First Stage fluid algorithms."
        ),
        (
            "Develop propulsion systems for the reusable first stage, upper "
            "stage, kick-stage and all stages of the launcher."
        ),
    ],
)
def test_positive_control_context_families_remain_clear(description: str) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "The role is full-time employment, with no contractor arrangements available.",
        "We do not hire freelancers for this permanent role.",
        "Freelance status is not available for this position.",
        "Nous ne recrutons pas de freelance pour ce poste en CDI.",
    ],
)
def test_negated_incompatible_contract_is_not_a_blocker(description: str) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "Contrat : Freelance, Portage, CDI",
        "Type de contrat : [CDI / Freelance]",
        "Le recrutement est proposé en CDD ou en CDI.",
        "Statut : FREELANCE / ESN ou Salarié CDD - CDI selon profil.",
        "Modalités : CDI à Lille ou Freelance en remote.",
    ],
)
def test_compatible_contract_menu_is_unresolved(description: str) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    assert decision.conflicts


@pytest.mark.parametrize(
    ("description", "expected_reason"),
    [
        (
            "This is a full-time CDD contract until Summer 2027.",
            "blocked_contract_visible_text:cdd",
        ),
        (
            "Type de contrat Alternance",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "Ce poste est à pourvoir en CDD jusqu'à la fin de l'année.",
            "blocked_contract_visible_text:cdd",
        ),
    ],
)
def test_explicit_exclusive_contract_remains_blocked(
    description: str,
    expected_reason: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (expected_reason,)


def test_safe_past_experience_does_not_hide_a_real_contract_clause() -> None:
    decision = evaluate_contract(
        _context(
            description=(
                "Previous internship experience is accepted. "
                "This is an internship role focused on applied ML."
            ),
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert "blocked_contract_visible_text:internship" in decision.reason_codes


def test_historical_context_before_current_offer_does_not_hide_internship() -> None:
    decision = evaluate_contract(
        _context(
            description=(
                "Previous experience is preferred, and this is an internship role."
            ),
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("blocked_contract_visible_text:internship",)


@pytest.mark.parametrize(
    ("description", "expected_reason"),
    [
        (
            "Previous experience is preferred, and we offer a six-month "
            "internship in Paris.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "Une expérience préalable est appréciée, et nous proposons un "
            "stage de 6 mois.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "Our team values prior experience and offers an internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "The role includes managing interns and is an internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "This role manages our internship program, while this position "
            "is an internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "The team includes an intern, whereas this opportunity is an "
            "internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "You supervise apprentices | this role is an apprenticeship.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "The role manages an internship program: this position is an "
            "internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "This role manages our internship program, however this position "
            "is an internship.",
            "blocked_contract_visible_text:internship",
        ),
    ],
)
def test_false_context_in_one_predicate_does_not_hide_current_offer(
    description: str,
    expected_reason: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (expected_reason,)


@pytest.mark.parametrize(
    "description",
    [
        "Previous internship experience is preferred.",
        "The role includes managing interns.",
    ],
)
def test_false_context_without_current_contract_signal_stays_clear(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


def test_candidate_joining_team_keeps_fixed_term_contract_visible() -> None:
    decision = evaluate_contract(
        _context(
            description=(
                "Join our team as a Data Scientist on a 12-month fixed-term contract."
            ),
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("blocked_contract_visible_text:cdd",)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("AI Research", "About the Internship\nDuring this internship, you will build models."),
        ("Off-Cycle Analyst", "Quantitative Research, Off-Cycle Intern"),
        ("Big data analyses", "Internship"),
        ("Analyse par IA", "Stage\nCe stage porte sur des modèles prédictifs."),
        ("[STAGE 2027] Data Scientist", "Nous recherchons un stagiaire."),
    ],
)
def test_explicit_internship_overrides_permanent_metadata(
    title: str,
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(title=title, description=description, contract_type="CDI")
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes
    assert not decision.conflicts
    assert {item.rule_id for item in decision.evidence} == {
        "contract.visible_incompatible",
    }


def test_temporary_staffing_activity_does_not_hide_a_real_cdd_clause() -> None:
    decision = evaluate_contract(
        _context(
            description=(
                "Notre entreprise est spécialiste du travail temporaire. "
                "Ce poste est en CDD de 12 mois."
            ),
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert "blocked_contract_visible_text:cdd" in decision.reason_codes


@pytest.mark.parametrize(
    ("contract_type", "facts", "expected_note"),
    [
        ("Contract", FilterFacts(), "contract_type_uncorroborated:contract"),
        (
            None,
            FilterFacts(
                source="serpapi",
                structured_contract_type="Prestataire",
                structured_contract_source="serpapi:schedule_type",
            ),
            "structured_contract_uncorroborated:prestataire",
        ),
    ],
)
def test_ambiguous_contract_labels_need_corroboration(
    contract_type: str | None,
    facts: FilterFacts,
    expected_note: str,
) -> None:
    decision = evaluate_contract(
        _context(facts=facts, contract_type=contract_type)
    )

    assert decision.status is GateStatus.CLEAR
    assert expected_note in decision.notes


def test_generic_contract_role_visible_text_is_not_freelance_proof() -> None:
    decision = evaluate_contract(
        _context(
            contract_type=None,
            description="This is a contract role building machine-learning models.",
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_platform_url_alone_does_not_prove_freelance_status() -> None:
    decision = evaluate_contract(
        _context(
            facts=FilterFacts(source="serpapi"),
            contract_type=None,
            application_url="https://www.free-work.com/fr/tech-it/data/job-mission/1",
        )
    )

    assert decision.status is GateStatus.CLEAR


def test_generic_contract_label_role_and_platform_url_still_need_evidence() -> None:
    decision = evaluate_contract(
        _context(
            contract_type="Contract",
            description="This is a contract role building machine-learning models.",
            application_url="https://www.free-work.com/fr/tech-it/data/job-mission/1",
        )
    )

    assert decision.status is GateStatus.CLEAR
    assert "contract_type_uncorroborated:contract" in decision.notes


@pytest.mark.parametrize(
    "source_data",
    [
        {"detected_extensions": {"salary": "€600 per day"}},
        {
            "compensation": {
                "amount": 75,
                "currency": "EUR",
                "period": "DAY",
            }
        },
    ],
)
def test_structured_daily_pay_corroborates_contract_label(
    source_data: dict[str, Any],
) -> None:
    decision = evaluate_contract(
        _context(contract_type="Contract", source_data=source_data)
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (
        "blocked_contract_type:contract (tag 'contractor')",
    )
    assert any(
        note.startswith("contractor_corroboration:structured_pay:source_data.")
        for note in decision.notes
    )


def test_hourly_wage_does_not_turn_ambiguous_contract_into_freelance() -> None:
    decision = evaluate_contract(
        _context(
            contract_type="Contract",
            description="This is a permanent full-time position.",
            source_data={
                "compensation": {"amount": 40, "currency": "EUR", "period": "HOUR"}
            },
        )
    )

    assert decision.status is GateStatus.CLEAR
    assert "contract_type_uncorroborated:contract" in decision.notes


@pytest.mark.parametrize(
    "description",
    [
        "Ce poste est en CDD. Nos consultants sont en CDI.",
        "This is an internship role. Our permanent employees receive benefits.",
    ],
)
def test_third_party_permanent_wording_does_not_cancel_candidate_blocker(
    description: str,
) -> None:
    decision = evaluate_contract(_context(contract_type=None, description=description))

    assert decision.status is GateStatus.BLOCKED


@pytest.mark.parametrize(
    "source_data",
    [
        {"salary": {"min": 70000, "currency": "EUR", "period": "yearly"}},
        {"description": "The client budget can reach €600 per day."},
    ],
)
def test_non_rate_or_unstructured_pay_text_does_not_corroborate_contract_label(
    source_data: dict[str, Any],
) -> None:
    decision = evaluate_contract(
        _context(contract_type="Contract", source_data=source_data)
    )

    assert decision.status is GateStatus.CLEAR
    assert "contract_type_uncorroborated:contract" in decision.notes


@pytest.mark.parametrize(
    "application_url",
    [
        None,
        "https://www.free-work.com/fr/tech-it/data/job-mission/1",
        "https://another-marketplace.example/jobs/1",
    ],
)
def test_marketplace_hostname_does_not_change_ambiguous_contract_decision(
    application_url: str | None,
) -> None:
    decision = evaluate_contract(
        _context(
            facts=FilterFacts(
                source="serpapi",
                structured_contract_type="Prestataire",
                structured_contract_source="serpapi:schedule_type",
            ),
            contract_type=None,
            application_url=application_url,
        )
    )

    assert decision.status is GateStatus.CLEAR
    assert "structured_contract_uncorroborated:prestataire" in decision.notes


@pytest.mark.parametrize(
    ("contract_type", "facts", "application_url"),
    [
        ("Contract", FilterFacts(), None),
        (
            None,
            FilterFacts(
                source="serpapi",
                structured_contract_type="Prestataire",
                structured_contract_source="serpapi:schedule_type",
            ),
            "https://www.free-work.com/fr/tech-it/data/job-mission/1",
        ),
    ],
)
def test_ambiguous_contract_labels_are_blocked_once_corroborated(
    contract_type: str | None,
    facts: FilterFacts,
    application_url: str | None,
) -> None:
    decision = evaluate_contract(
        _context(
            facts=facts,
            contract_type=contract_type,
            description="This is a contractor role building ML models.",
            application_url=application_url,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.evidence


@pytest.mark.parametrize(
    "facts",
    [
        FilterFacts(
            source="serpapi",
            structured_contract_type="Internship",
            structured_contract_source="serpapi:schedule_type",
        ),
        FilterFacts(
            source="francetravail",
            structured_alternance=True,
        ),
    ],
)
def test_structured_incompatible_contract_vs_visible_cdi_is_unresolved(
    facts: FilterFacts,
) -> None:
    decision = evaluate_contract(
        _context(
            facts=facts,
            description="Type de contrat : CDI. Build ML models with Python.",
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    assert decision.conflicts
    assert {item.source for item in decision.evidence} >= {"job.description"}


def test_conflicting_contract_does_not_cancel_independent_part_time_blocker() -> None:
    decision = evaluate_contract(
        _context(
            facts=FilterFacts(
                source="serpapi",
                structured_contract_type="Internship",
                structured_contract_source="serpapi:schedule_type",
                structured_work_time="Part-time",
                structured_work_time_source="serpapi:schedule_type",
            ),
            description="Type de contrat : CDI. Build ML models with Python.",
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (
        "blocked_work_time_structured:part-time (tag 'part_time')",
    )
    assert not decision.conflicts
    assert "contract_conflict_non_decisive" in decision.notes


@pytest.mark.parametrize(
    ("title", "expected_reason"),
    [
        ("Data Scientist - CDD", "blocked_contract_visible_text:cdd"),
        ("Stage - Data Scientist", "blocked_contract_visible_text:stage"),
        (
            "Freelance Data Scientist",
            "blocked_contract_visible_text:freelance",
        ),
    ],
)
def test_explicit_title_contract_overrides_permanent_metadata(
    title: str,
    expected_reason: str,
) -> None:
    decision = evaluate_contract(
        _context(
            title=title,
            facts=FilterFacts(
                source="serpapi",
                structured_contract_type="Permanent",
                structured_contract_source="serpapi:schedule_type",
            ),
            contract_type="CDI",
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (expected_reason,)
    assert not decision.conflicts
    assert {item.source for item in decision.evidence} == {"job.title"}


@pytest.mark.parametrize(
    "description",
    [
        "Type de contrat : CDD. Développer des modèles ML.",
        "Ce poste est en CDD de 12 mois pour développer des modèles ML.",
        "Position: Internship. Build machine-learning models.",
        "Statut : Freelance. Build machine-learning models.",
    ],
)
def test_offer_specific_contract_overrides_permanent_metadata(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(
            facts=FilterFacts(
                source="serpapi",
                structured_contract_type="Permanent",
                structured_contract_source="serpapi:schedule_type",
            ),
            description=description,
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes
    assert not decision.conflicts
    assert {item.source for item in decision.evidence} == {"job.description"}


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Data Scientist - CDD",
            "Type de contrat : CDI. Développer des modèles ML.",
        ),
        (
            "Stage - Data Scientist",
            "Type de contrat : CDI. Développer des modèles ML.",
        ),
        (
            "Freelance Data Scientist",
            "Type de contrat : CDI. Développer des modèles ML.",
        ),
        (
            "Data Scientist",
            "Le recrutement est proposé en CDD ou en CDI.",
        ),
        (
            "Data Scientist",
            "Le recrutement est proposé en CDI ou en CDD.",
        ),
    ],
)
def test_direct_cdd_and_direct_cdi_wording_remain_unresolved(
    title: str,
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(
            title=title,
            description=description,
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    expected_sources = (
        {"job.title", "job.description"}
        if title != "Data Scientist"
        else {"job.description"}
    )
    assert {item.source for item in decision.evidence} == expected_sources


@pytest.mark.parametrize(
    "description",
    [
        "Poste à temps plein ou à temps partiel selon votre préférence.",
        "Poste CDI à temps plein; candidatures à temps partiel possibles.",
        "Full-time position; part-time available on request.",
    ],
)
def test_optional_schedule_with_a_full_time_path_is_not_rejected(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "Nos consultants sont en CDD.",
        "L'équipe comprend plusieurs consultants en CDD et des stagiaires.",
        "Vous gérez les contrats CDD des collaborateurs.",
        "Poste : encadrement de stagiaires.",
        "Role: manage a team of interns.",
    ],
)
def test_contracts_and_schedules_of_third_parties_do_not_block(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "This role manages our internship program and builds predictive models.",
        "You will be responsible for the internship program and build predictive models.",
    ],
)
def test_internship_program_responsibility_is_not_candidate_contract(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    ("description", "expected_reason"),
    [
        (
            "The role is part-time and includes managing a team of interns.",
            "blocked_work_time_visible_text:part_time",
        ),
        (
            "Le poste est en CDD et comprend l'encadrement de stagiaires.",
            "blocked_contract_visible_text:cdd",
        ),
        (
            "Ce poste à temps partiel implique de manager une équipe.",
            "blocked_work_time_visible_text:part_time",
        ),
        (
            "You will manage a team in this part-time role.",
            "blocked_work_time_visible_text:part_time",
        ),
    ],
)
def test_third_party_duties_do_not_hide_candidate_contract(
    description: str,
    expected_reason: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.BLOCKED
    assert expected_reason in decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "Niveau : junior, intermédiaire ou stage de fin d'études.",
        "Niveau : stage de fin d'études, junior ou intermédiaire.",
        "Profils acceptés : junior, intermédiaire ou stage de fin d'études.",
        "Profils acceptés : stage de fin d'études ou junior.",
        "Ouvert aux profils stage de fin d'études comme aux profils junior.",
        "Open to internship profiles as well as junior profiles.",
        "Accepted applicants: internship or entry-level.",
        "Profil junior bienvenu, stage de fin d'études ou alternance inclus.",
    ],
)
def test_experience_level_menu_is_order_invariant(description: str) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR


def test_mandatory_part_time_wording_is_not_masked_as_an_option() -> None:
    decision = evaluate_contract(
        _context(
            description="Only part-time applications are accepted.",
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("blocked_work_time_visible_text:part_time",)


def test_structured_part_time_vs_direct_full_time_is_unresolved() -> None:
    decision = evaluate_contract(
        _context(
            facts=FilterFacts(
                source="job_board",
                structured_work_time="Part-time",
                structured_work_time_source="job_board:schedule",
            ),
            description="This is a full-time position building ML models.",
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    assert decision.conflicts
    assert {item.source for item in decision.evidence} == {
        "job_board:schedule",
        "job.description",
    }


def test_mixed_internship_level_menu_vs_full_time_metadata_is_unresolved() -> None:
    decision = evaluate_contract(
        _context(
            title="Ingénieur Full Stack IoT Cloud Data",
            description=(
                "Expérience\n·\nNiveau\n: Stage de fin d’études, junior "
                "ou intermédiaire\n·\nFormation : Bac +5 en informatique"
            ),
            contract_type="Full-time",
            facts=FilterFacts(
                source="linkedin",
                structured_contract_type="Full-time",
                structured_contract_source="linkedin:contractType",
            ),
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    assert decision.conflicts
    assert {item.source for item in decision.evidence} == {
        "job.description",
        "job.contract_type",
        "linkedin:contractType",
    }


def test_implausible_one_hour_week_source_fact_is_unresolved() -> None:
    decision = evaluate_contract(
        _context(
            facts=FilterFacts(
                source="francetravail",
                structured_contract_type="CDI",
                structured_contract_source="francetravail:typeContratLibelle",
                structured_work_time="Temps partiel",
                structured_work_time_source=(
                    "francetravail:dureeTravailLibelleConverti"
                ),
                warnings=["structured_work_time_implausible_weekly_hours:1"],
            ),
            contract_type="CDI",
            source_data={
                "dureeTravailLibelle": "Temps partiel - 1H/semaine\nTravail en journée",
                "dureeTravailLibelleConverti": "Temps partiel",
            },
        )
    )

    assert decision.status is GateStatus.UNRESOLVED
    assert not decision.reason_codes
    assert decision.conflicts
    assert any(
        item.rule_id == "contract.source_fact_implausible_work_time"
        for item in decision.evidence
    )


def test_visible_part_time_still_blocks_with_implausible_source_duration() -> None:
    decision = evaluate_contract(
        _context(
            description="Ce poste est proposé à temps partiel.",
            facts=FilterFacts(
                source="francetravail",
                structured_work_time="Temps partiel",
                structured_work_time_source="francetravail:dureeTravailLibelle",
                warnings=["structured_work_time_implausible_weekly_hours:1"],
            ),
            contract_type=None,
            source_data={"dureeTravailLibelle": "1H/semaine"},
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("blocked_work_time_visible_text:part_time",)


@pytest.mark.parametrize(
    "title",
    [
        "Independent Model Validation Analyst",
        "Independent Research Scientist",
        "Independent Verification and Validation Engineer",
        "Independent Model Risk Analyst",
    ],
)
def test_independent_function_title_is_not_a_contract_signal(title: str) -> None:
    decision = evaluate_contract(
        _context(
            title=title,
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "The role is independent of the finance organization.",
        "Opportunity for an Independent Model Validation Analyst.",
    ],
)
def test_independent_adjective_is_not_a_contract_signal(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Independent Consultant", "Build machine learning models."),
        ("Consultant indépendant", "Développer des modèles prédictifs."),
        ("Data Scientist", "Statut : indépendant."),
        ("Data Scientist", "This is a self-employed role."),
    ],
)
def test_explicit_independent_status_remains_incompatible(
    title: str,
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(
            title=title,
            description=description,
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (
        "blocked_contract_visible_text:independant",
    )


@pytest.mark.parametrize(
    "description",
    [
        "This role is part of the next stage of our growth.",
        "The position is key to the implementation stage.",
        "The role is at a mature stage of development.",
        "This is a stage-gate analytics role.",
    ],
)
def test_process_stage_is_not_an_internship_signal(description: str) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


def test_stage_gate_title_is_not_an_internship_signal() -> None:
    decision = evaluate_contract(
        _context(title="Stage-Gate Process Analyst", contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "The role is designed to automate temporary data pipelines.",
        "The role is responsible for interim analysis of clinical data.",
        "The position is central to freelance vendor management.",
        "This job is to improve our apprenticeship recommendation platform.",
        "The position is based on a fixed-term forecasting horizon.",
    ],
)
def test_offer_frame_does_not_capture_distant_contract_words(
    description: str,
) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    ("field", "text", "expected_reason"),
    [
        (
            "description",
            "This role is a freelance contract.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "This role is a six-month internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "description",
            "Position: internship.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "description",
            "Position available for an intern in our AI team.",
            "blocked_contract_visible_text:internship",
        ),
        (
            "description",
            "Opportunity for a freelance Machine Learning Engineer.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "Opportunité pour un freelance en machine learning.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "Mission ouverte à un indépendant pour développer des modèles IA.",
            "blocked_contract_visible_text:independant",
        ),
        (
            "description",
            "Recherche stagiaire Data Scientist.",
            "blocked_contract_visible_text:stage",
        ),
        (
            "description",
            "Recherche alternant Data Scientist.",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "description",
            "Recherche freelance Machine Learning Engineer.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "Data Scientist en CDD.",
            "blocked_contract_visible_text:cdd",
        ),
        (
            "description",
            "Opportunity for a contract Machine Learning Engineer.",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "Statut : Freelance",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "Statut du poste : indépendant",
            "blocked_contract_visible_text:independant",
        ),
        (
            "description",
            "Employment type: Internship",
            "blocked_contract_visible_text:internship",
        ),
        (
            "description",
            "Job type: Internship",
            "blocked_contract_visible_text:internship",
        ),
        (
            "description",
            "Mode de collaboration : freelance",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "Freelance only",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "description",
            "Contrat pro de 12 mois",
            "blocked_contract_visible_text:alternance",
        ),
        (
            "title",
            "Opportunity for a freelance Machine Learning Engineer",
            "blocked_contract_visible_text:freelance",
        ),
        (
            "title",
            "Opportunity for a contract Machine Learning Engineer",
            "blocked_contract_visible_text:freelance",
        ),
    ],
)
def test_local_candidate_contract_phrasing_remains_incompatible(
    field: str,
    text: str,
    expected_reason: str,
) -> None:
    values: dict[str, Any] = {field: text, "contract_type": None}
    decision = evaluate_contract(_context(**values))

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == (expected_reason,)


def test_professionalization_contract_is_incompatible_for_cdi_only_profile() -> None:
    decision = evaluate_contract(
        _context(
            description=(
                "Ce poste est proposé dans le cadre d’un contrat de "
                "professionnalisation."
            ),
            contract_type=None,
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert decision.reason_codes == ("blocked_contract_visible_text:alternance",)


@pytest.mark.parametrize(
    "description",
    [
        "Our careers page covers permanent, fixed-term and internship opportunities.",
        "Previous internship or fixed-term contract experience is welcome.",
    ],
)
def test_weak_contract_boilerplate_does_not_block(description: str) -> None:
    decision = evaluate_contract(
        _context(description=description, contract_type=None)
    )

    assert decision.status is GateStatus.CLEAR
    assert not decision.reason_codes


@pytest.mark.parametrize(
    "title",
    [
        "Doctorant Data Scientist",
        "Data Scientist - Thèse CIFRE",
        "Ph.D. Research Engineer",
    ],
)
def test_doctoral_title_keeps_the_cdd_exception(title: str) -> None:
    decision = evaluate_contract(
        _context(title=title, contract_type="CDD temps plein")
    )

    assert decision.status is GateStatus.CLEAR
    assert "contract_exception:research_degree_title_cdd" in decision.notes
    assert "contract_ok:cdd temps plein:research_degree_title" in decision.notes


def test_doctoral_marker_in_description_does_not_enable_cdd_exception() -> None:
    decision = evaluate_contract(
        _context(
            title="Data Scientist",
            description="Environnement de recherche doctorale et modèles ML.",
            contract_type="CDD",
        )
    )

    assert decision.status is GateStatus.BLOCKED


def test_doctoral_title_keeps_visible_fixed_term_exception_with_full_time_metadata() -> None:
    decision = evaluate_contract(
        _context(
            title="AI Research Assistant (PhD)",
            description="This is a three-year fixed term research contract.",
            contract_type="Full-time",
        )
    )

    assert decision.status is GateStatus.CLEAR
    assert "contract_exception:research_degree_title_cdd" in decision.notes


def test_doctoral_title_does_not_allow_an_internship() -> None:
    decision = evaluate_contract(
        _context(title="PhD Research Engineer", contract_type="Stage")
    )

    assert decision.status is GateStatus.BLOCKED
    assert any("internship" in reason for reason in decision.reason_codes)


def test_future_cdi_after_stage_is_not_treated_as_a_current_conflict() -> None:
    decision = evaluate_contract(
        _context(
            description="Stage de 6 mois avec possibilité de CDI après le stage.",
            contract_type="Stage",
        )
    )

    assert decision.status is GateStatus.BLOCKED
    assert not decision.conflicts
