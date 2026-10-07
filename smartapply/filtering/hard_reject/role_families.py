"""Title-family evidence for the deterministic role gate.

A title can prove that an occupation is outside the requested field only for
very explicit families (sales, accounting, construction, and similar work).
Technical neighbours such as software, data engineering, infrastructure, or
cybersecurity are deliberately deferred to the later relevance classifier.
This policy prevents the hard gate from depending on an ever-growing list of
AI wording exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TitleFamilyPolicy(str, Enum):
    """What the hard gate may conclude from a title-family match."""

    HARD_REJECT = "hard_reject"
    DEFER_TO_RELEVANCE = "defer_to_relevance"


@dataclass(frozen=True)
class TitleFamily:
    """One occupation family and the strongest decision its title supports."""

    name: str
    policy: TitleFamilyPolicy
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class TitleFamilyMatch:
    """Auditable match returned to the role gate."""

    name: str
    policy: TitleFamilyPolicy


TITLE_FAMILIES: tuple[TitleFamily, ...] = (
    TitleFamily(
        "sales_commercial",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:chargee?\s+d[' ]?affaires|ingenieur\s+commercial|commercial|"
            r"ingenieur\s+avant[- ]ventes?|account\s+executive|"
            r"business\s+development|sales\s+(?:representative|manager|executive|"
            r"consultant|engineer))\b"
        ),
    ),
    TitleFamily(
        "business_analysis_operations",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:business\s+analyst|economic\s+(?:and|&)\s+impact\s+analyst|"
            r"gtm|go[- ]to[- ]market|revops|revenue\s+operations?|"
            r"sales\s+operations?|growth\s+operations?|operations\s+coordinator)\b"
        ),
    ),
    TitleFamily(
        "building_hvac",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:cvc|hvac|genie\s+climatique|plomberie|froid\s+industriel|"
            r"ventilation|thermique\s+du\s+batiment|fluides\s+du\s+batiment)\b"
        ),
    ),
    TitleFamily(
        "industrial_methods_maintenance",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:ingenieur(?:e)?\s+methodes?(?:\s+industrielles?)?|"
            r"methodes?\s+(?:maintenance|production|industrialisation)|"
            r"maintenance\s+(?:industrielle|multi[- ]sites?|cvc)|"
            r"responsable\s+maintenance|ingenieur(?:e)?\s+maintenance|"
            r"ingenieur(?:e)?\s+industriel|ingenieur(?:e)?\s+process|"
            r"ingenieur(?:e)?\s+(?:maitrise\s+des\s+)?procedes?|"
            r"installation\s+generale|ingenieur(?:e)?\s+de\s+production|"
            r"adjoint(?:e)?\s+methodes?.{0,35}\bproduction|"
            r"fleet\s+performance|flight\s+test|gmao)\b"
        ),
    ),
    TitleFamily(
        "control_automation",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:controle\s+commande|control\s+systems?|automaticien(?:ne)?|"
            r"automatisme|signalisation\s+ferroviaire|scada|gtb|gtc|"
            r"automate\s+programmable|plc)\b"
        ),
    ),
    TitleFamily(
        "hardware_electronics",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:hardware|electronique|electronic(?:s)?|electrotechnique|"
            r"microelectroniques?|radiofrequence|radio\s+frequence|"
            r"circuits?\s+integres?|architecture\s+electrique|haute\s+tension|"
            r"qubits?|calcul\s+quantique|cmos|rf\s+engineer|fpga|"
            r"hyperfrequences?|antennes?\s+millimetriques?|optique|"
            r"systemes?\s+embarques?|embedded\s+systems?|mecatronique)\b"
        ),
    ),
    TitleFamily(
        "mechanical_civil_materials",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:ingenieur(?:e)?\s+(?:mecanique|structure|structures|civil|vrd)|"
            r"bureau\s+d[' ]?etudes?\s+mecanique|mecanicien|geotechni(?:que|cien)|"
            r"genie\s+civil|metallurgie|machinability|materiaux|materials?\s+engineer|"
            r"calcul\s+(?:de\s+)?(?:structures?|dynamique)|"
            r"dynamique\s+des\s+structures?|thermique|thermodynamique|fluides?|"
            r"radiation|cao|tuyauterie|chaudronnerie)\b"
        ),
    ),
    TitleFamily(
        "infrastructure_operations",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:cloud\s+infrastructure|infrastructure\s+engineer|sysops|"
            r"infrastructure\s+informatique|google\s+cloud\s+architect|"
            r"site\s+reliability|sre|devops|network\s+engineer|"
            r"ingenieur\s+reseaux?|stockage\s+et\s+virtualisation|"
            r"datacenter\s+hardware|platform\s+engineer|"
            r"finops|sauvegarde|backup)\b"
        ),
    ),
    TitleFamily(
        "quality_validation_safety",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:quality(?:\s+assurance)?\s+engineer|ingenieur(?:e)?\s+qualite|"
            r"analyste\s+qualite\s+logiciel|ingenieur(?:e)?\s+qa(?:\s+automatisation)?|"
            r"ivq|ivvq|validation\s+engineer|"
            r"safety\s*(?:and|&)\s*reliability|safety\s+assessor|"
            r"surete\s+(?:de\s+fonctionnement|nucleaire|des\s+procedes)|"
            r"chemical\s+safety|"
            r"explosives?\s+(?:and\s+)?energetic\s+materials?\s+expert|"
            r"rams\s+engineer|test\s+engineer|charge\s+de\s+test|"
            r"ingenieur(?:e)?\s+(?:de\s+)?tests?|ingenieur(?:e)?\s+d[' ]?essais|"
            r"teststand)\b"
        ),
    ),
    TitleFamily(
        "data_platform",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:data\s+engineer(?:ing)?|dataops|data\s+platform\s+engineer|"
            r"ingenieur(?:e)?\s+(?:data|de\s+donnees)|"
            r"ingenieur(?:e)?\s+data.{0,35}\b(?:etl|informatica|semarchy)|"
            r"ingenierie\s+(?:data|des\s+donnees))\b"
        ),
    ),
    TitleFamily(
        "reporting_bi",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:reporting\s+analyst|analyste\s+reporting|bi\s+analyst|"
            r"analyste\s+programmeur\s+decisionnel)\b"
        ),
    ),
    TitleFamily(
        "web_analytics_tracking",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:web\s+analytics\s+(?:specialist|consultant)|"
            r"specialiste\s+webanalyse|tracking\s+specialist)\b"
        ),
    ),
    TitleFamily(
        "project_product_management",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:product\s+owner|proxy\s+product\s+owner|"
            r"digital\s+product\s+owner|chef(?:fe)?\s+de\s+projet|"
            r"chargee?\s+de\s+projets?|product\s+(?:delivery\s+)?manager|"
            r"project\s+manager|programme?\s+manager|pmo)\b"
        ),
    ),
    TitleFamily(
        "laboratory_domain_science",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:ingenieur(?:e)?\s+en\s+genetique|"
            r"laboratoire\s+(?:(?:et|&)\s+)?qualite|"
            r"research\s+engineer\s+in\s+(?:machinability|metallurgy)|"
            r"clinical\s+scientist|psychologist|entomologiste|agronome|biochimie|"
            r"biogeochimie|phytoplancton|lipase|geologue|geophysicien|plasma)\b"
        ),
    ),
    TitleFamily(
        "legal_compliance",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:legal|lawyer|compliance|intelligence\s+economique|"
            r"privacy\s+officer|data\s+protection\s+(?:analyst|officer)|juriste|"
            r"conformite|responsable\s+rgpd|"
            r"delegue\s+a\s+la\s+protection\s+des\s+donnees)\b"
        ),
    ),
    TitleFamily(
        "cybersecurity_identity",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:cyber(?:security|securite)|security\s+(?:engineer|consultant)|"
            r"cyber\s+resilience|identity\s+(?:and|&)\s+access|iam(?:/pam)?|pam|"
            r"waf|identites?\s*(?:et|&)\s*acces|identite\s+numerique|soc)\b"
        ),
    ),
    TitleFamily(
        "academic_teaching",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:assistant\s+professor|enseignant(?:e)?|teacher|instructor|"
            r"digital\s+learning)\b"
        ),
    ),
    TitleFamily(
        "training_ambiguous",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(r"\b(?:trainers?|formateur|formatrice)\b"),
    ),
    TitleFamily(
        "industrial_studies_operations",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:etude\s+de\s+prix|chargee?\s+d[' ]?etudes?\s+(?:thermiques?|"
            r"incendie|concertation|environnement)|"
            r"chargee?\s+d[' ]?etudes?.{0,35}\bincendie|"
            r"operateur\s+de\s+fabrication|ingenieur\s+sav|aeroport|"
            r"batiments?\s+departementaux|robinetterie|icpe)\b"
        ),
    ),
    TitleFamily(
        "geospatial_surveying",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(r"\b(?:surveying|topography|topographie|geomatique|geomaticien)\b"),
    ),
    TitleFamily(
        "people_administration",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:human\s+resources|talent\s+acquisition|recruiter|office\s+manager|"
            r"administrative\s+assistant|ressources\s+humaines|recruteur|recruteuse|"
            r"assistant(?:e)?\s+administrati(?:f|ve))\b"
        ),
    ),
    TitleFamily(
        "people_analytics",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(r"\b(?:hr\s+analytics|people\s+analytics)\b"),
    ),
    TitleFamily(
        "marketing_customer",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:marketing\s+(?:manager|specialist|coordinator)|customer\s+success|"
            r"customer\s+support|communications?\s+manager|chargee?\s+de\s+marketing|"
            r"responsable\s+marketing|relation\s+client|support\s+client|"
            r"chargee?\s+de\s+communication)\b"
        ),
    ),
    TitleFamily(
        "marketing_analytics",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(r"\b(?:marketing\s+analyst|pricing\s+marketing)\b"),
    ),
    TitleFamily(
        "support_and_administration",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(
            r"\b(?:helpdesk|hotline|exploitation\s+applicative|documentaliste|"
            r"comptable|comptabilite|data\s+entry|saisie\s+de\s+donnees|"
            r"database\s+administrator|administrateur\s+de\s+base\s+de\s+donnees|"
            r"infirmier|infirmiere|nurse)\b"
        ),
    ),
    TitleFamily(
        "data_literal_not_analytics",
        TitleFamilyPolicy.HARD_REJECT,
        re.compile(r"\b(?:data\s+center|data\s+centre|centre\s+de\s+donnees)\b"),
    ),
    TitleFamily(
        "general_software_development",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(
            r"\b(?:software\s+engineer|ingenieur(?:e)?\s+logiciel|"
            r"developpeur|developpeuse|full[- ]?stack|backend|frontend|"
            r"ingenieur(?:e)?\s+(?:d[' ]?)?etudes?\s+et\s+developpement|"
            r"analyste\s+programmeur|architecte?|solutions?\s+architect|expert)\b"
        ),
    ),
    TitleFamily(
        "actuarial_analytics",
        TitleFamilyPolicy.DEFER_TO_RELEVANCE,
        re.compile(r"\bactuariell?e?s?\b"),
    ),
)


def match_title_families(title: str) -> tuple[TitleFamilyMatch, ...]:
    """Return every distinct family named by the normalized visible title."""

    matches: list[TitleFamilyMatch] = []
    seen: set[str] = set()
    for family in TITLE_FAMILIES:
        if family.name not in seen and family.pattern.search(title):
            seen.add(family.name)
            matches.append(TitleFamilyMatch(family.name, family.policy))
    return tuple(matches)


# Compatibility exports for callers that only need the old first-match view.
OFF_TARGET_TITLE_FAMILIES = tuple((item.name, item.pattern) for item in TITLE_FAMILIES)


def off_target_title_family(title: str) -> str | None:
    matches = match_title_families(title)
    return matches[0].name if matches else None


__all__ = [
    "OFF_TARGET_TITLE_FAMILIES",
    "TITLE_FAMILIES",
    "TitleFamilyMatch",
    "TitleFamilyPolicy",
    "match_title_families",
    "off_target_title_family",
]
