"""Archive-reason normalization for storage and product-facing copy."""

from __future__ import annotations

import re
from collections.abc import Iterable

_NON_DECISIVE_REASON_PREFIXES = {
    "contract_ok",
    "contract_structured_uncorroborated",
    "offer_language",
    "positive_title",
    "remote_ok",
    "remote_structured",
    "role_concepts",
    "role_evidence",
    "role_relevance",
    "role_relevance_score",
    "rome_context",
    "search_context",
    "target_role",
    "work_time_structured",
}

_TITLE_FAMILY_LABELS = {
    "academic_teaching": "Poste d’enseignement, hors cible Data/IA",
    "building_hvac": "Poste orienté bâtiment, CVC ou génie climatique, hors cible Data/IA",
    "business": "Poste orienté business, hors cible Data/IA",
    "data_literal_not_analytics": "Poste lié aux data centers physiques, hors cible Data/IA",
    "data_engineering": "Poste de Data Engineering pur, hors cible actuelle",
    "devops": "Poste DevOps, hors cible Data/IA",
    "industrial_methods_maintenance": "Poste de méthodes ou maintenance industrielle, hors cible Data/IA",
    "industrial_studies_operations": "Poste d’études ou d’opérations industrielles, hors cible Data/IA",
    "laboratory_domain_science": "Poste scientifique spécialisé hors du domaine Data/IA recherché",
    "legal_compliance": "Poste juridique ou conformité, hors cible Data/IA",
    "marketing_customer": "Poste marketing ou relation client, hors cible Data/IA",
    "mechanical_civil_materials": "Poste mécanique, génie civil ou matériaux, hors cible Data/IA",
    "mlops": "Poste MLOps pur, hors cible actuelle",
    "people_administration": "Poste RH ou administratif, hors cible Data/IA",
    "product": "Poste produit, hors cible Data/IA",
    "sales_commercial": "Poste commercial ou vente, hors cible Data/IA",
    "software_engineering": "Poste de développement logiciel, hors cible Data/IA",
    "support_and_administration": "Poste support ou administration, hors cible Data/IA",
}

_LANGUAGE_LABELS = {
    "de": "allemand",
    "en": "anglais",
    "es": "espagnol",
    "fr": "français",
    "it": "italien",
}


def decisive_rejection_reasons(reasons: Iterable[object]) -> list[str]:
    """Remove evidence and collection context while preserving rejection causes."""
    decisive: list[str] = []
    for reason in reasons:
        value = str(reason).strip()
        if not value:
            continue
        prefix = value.split(":", 1)[0]
        if prefix in _NON_DECISIVE_REASON_PREFIXES or prefix.startswith("experience_structured_"):
            continue
        decisive.append(value)
    return decisive


def archive_reason_labels(
    reasons: Iterable[object],
    *,
    stage: str | None = None,
    archived: bool = False,
) -> tuple[str, ...]:
    """Return concise French labels for the offer detail panel."""
    values = decisive_rejection_reasons(reasons)
    if any(value.startswith("duplicate_reference:") for value in values):
        values = [value for value in values if not value.startswith("duplicate_of:")]

    labels: list[str] = []
    for value in values:
        label = _archive_reason_label(value)
        if label and label not in labels:
            labels.append(label)

    if labels:
        return tuple(labels)
    if not archived:
        return ()
    if stage == "manual":
        return ("Archivée manuellement",)
    return ("Offre écartée par les critères de sélection",)


def _archive_reason_label(reason: str) -> str:
    code, separator, raw_detail = reason.partition(":")
    detail = _clean_detail(raw_detail) if separator else ""

    if code == "manual_archive":
        return "Archivée manuellement"
    if code == "duplicate_reference":
        return f"Doublon de l’offre « {raw_detail.strip()} »"
    if code == "duplicate_of":
        return f"Offre déjà enregistrée (doublon n° {detail})"
    if code == "experience_required_too_high":
        years = re.search(r"\d+(?:[.,]\d+)?", raw_detail)
        suffix = f" : au moins {years.group(0)} ans" if years else ""
        return "Expérience demandée trop élevée" + suffix
    if code == "offer_language_not_accepted":
        language = _LANGUAGE_LABELS.get(detail, detail)
        return f"Langue de l’offre non prise en charge : {language}"
    if code.startswith("blocked_contract_"):
        contract = detail.split(" (tag", 1)[0].strip()
        return f"Type de contrat non recherché : {contract or 'non compatible'}"
    if code.startswith("blocked_work_time_"):
        return "Temps partiel non recherché"
    if code in {"title_hard_reject", "off_target_role_family"}:
        return _TITLE_FAMILY_LABELS.get(
            detail.replace(" ", "_"),
            f"Famille de poste hors cible : {detail or 'non reconnue'}",
        )
    if code == "qualification_required" and detail == "doctorate":
        return "Doctorat obligatoire non compatible avec le profil actuel"
    if code in {"seniority_in_title", "seniority_blocked"}:
        return (
            f"Niveau de séniorité trop élevé : {detail}"
            if detail
            else ("Niveau de séniorité trop élevé")
        )
    if code == "seniority_or_leadership_in_description":
        return "Niveau senior ou responsabilités managériales au-dessus du profil recherché"
    if code.startswith("deal_breaker_"):
        return f"Critère exclu détecté : {detail}"
    if code == "role_relevance_off_target":
        return "Poste insuffisamment lié à la Data, l’IA, au ML ou à l’analytics"
    if code == "web_analytics_tracking_focus":
        return "Poste centré sur le tracking web, sans responsabilité analytique suffisante"
    if code == "finance_reporting_bi_without_core_data_tech":
        return "Reporting financier/BI sans socle Data ou analytique suffisant"
    if code == "reporting_without_core_data_tech":
        return "Reporting sans Python ni technologie Data centrale"
    if code == "reporting_bi_without_analytical_ownership":
        return "Poste principalement orienté reporting/BI, sans responsabilité analytique réelle"
    if code == "pure_data_engineering_role":
        return "Poste de Data Engineering pur, sans composante analytique ou ML"
    if code == "mep_data_center_focus":
        return "Poste d’ingénierie MEP pour data center, hors cible Data/IA"
    if code == "analytics_without_python":
        return "Poste analytics sans Python ni responsabilité technique suffisante"
    if code in {"negative_desc_token", "negative_title", "contract_off"}:
        return f"Critère peu compatible avec le profil : {detail}"
    if code == "below_min_score":
        return "Correspondance globale insuffisante avec le profil"
    if code.startswith("location_rejected"):
        return "Localisation incompatible avec la recherche"
    return f"Motif de filtrage : {_clean_detail(reason)}"


def _clean_detail(value: str) -> str:
    return " ".join(value.replace("_", " ").split()).strip(" '​\"")
