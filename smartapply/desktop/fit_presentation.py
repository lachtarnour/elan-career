"""Descriptive fit presentation; never recalculates or repairs a model judgement."""

from smartapply.database.fit_ranking import current_policy_fingerprint

AXIS_LABELS = {
    "missions": "Missions",
    "essentials": "Essentiels techniques",
    "responsibilities": "Responsabilités",
}
DECISION_LABELS = {
    "very_low_support": "Très faible",
    "weak_support": "Faible",
    "partial_core_fit": "Partiel",
    "credible_fit": "Crédible",
    "strong_fit": "Fort",
}
STATUS_LABELS = {
    "compatible": "Compatible",
    "incompatible": "Incompatible",
    "unknown": "À confirmer",
}


def fit_summary(diagnostics, *, has_score: bool) -> dict:
    raw = diagnostics if isinstance(diagnostics, dict) else {}
    current = has_score and raw.get("policy_fingerprint") == current_policy_fingerprint()
    admissibility = raw.get("admissibility")
    admissibility = admissibility if isinstance(admissibility, dict) else {}
    statuses = []
    for dimension in ("mandatory_conditions", "candidate_preferences"):
        entry = admissibility.get(dimension)
        status = entry.get("status") if isinstance(entry, dict) else None
        statuses.append(status if isinstance(status, str) and status in STATUS_LABELS else "")
    candidacy = (
        "incompatible"
        if "incompatible" in statuses
        else "compatible"
        if statuses == ["compatible", "compatible"]
        else "to_verify"
    )
    return {
        "fit_current": current,
        "fit_status_label": (
            "Évaluation actuelle"
            if current
            else "Évaluation historique · à réévaluer"
            if has_score
            else ""
        ),
        "candidacy_status": candidacy if current else "",
        "candidacy_label": {
            "compatible": "Candidature envisageable",
            "to_verify": "Candidature à vérifier",
            "incompatible": "Incompatibilité confirmée",
        }[candidacy]
        if current
        else "",
    }


def fit_detail(diagnostics) -> dict:
    raw = diagnostics if isinstance(diagnostics, dict) else {}
    decisions = raw.get("used_decisions")
    decisions = decisions if isinstance(decisions, dict) else {}
    levels = raw.get("used_levels")
    levels = levels if isinstance(levels, dict) else {}
    axes = []
    for axis, label in AXIS_LABELS.items():
        decision = decisions.get(axis)
        level = levels.get(axis)
        axes.append(
            {
                "axis": axis,
                "label": label,
                "decision": decision if isinstance(decision, str) else "",
                "decision_label": DECISION_LABELS.get(decision, "Indisponible")
                if isinstance(decision, str)
                else "Indisponible",
                "level": level if type(level) is int and 0 <= level <= 4 else None,
            }
        )
    admissibility = raw.get("admissibility")
    admissibility = admissibility if isinstance(admissibility, dict) else {}
    dimensions = []
    for dimension, label in (
        ("mandatory_conditions", "Conditions obligatoires"),
        ("candidate_preferences", "Préférences du candidat"),
    ):
        entry = admissibility.get(dimension)
        entry = entry if isinstance(entry, dict) else {}
        status = entry.get("status")
        status = status if isinstance(status, str) and status in STATUS_LABELS else ""
        findings = entry.get("findings")
        findings = findings if isinstance(findings, list) else []
        texts = [
            item["text"]
            for item in findings
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
        ]
        dimensions.append(
            {
                "dimension": dimension,
                "label": label,
                "status": status,
                "status_label": STATUS_LABELS.get(status, "Information indisponible"),
                "findings": texts,
            }
        )
    reliability = raw.get("reliability")
    reliability = reliability if isinstance(reliability, dict) else {}
    descriptions = []
    for key, label in (
        ("offer_information", "Information disponible dans l’annonce"),
        ("candidate_information", "Information disponible dans le profil"),
        ("traceability", "Traçabilité"),
    ):
        value = reliability.get(key)
        descriptions.append(
            {
                "label": label,
                "text": value
                if isinstance(value, str) and value.strip()
                else "Information indisponible",
            }
        )
    uncertainties = reliability.get("business_uncertainties")
    if isinstance(uncertainties, list):
        descriptions.extend(
            {"label": "À confirmer", "text": item}
            for item in uncertainties
            if isinstance(item, str) and item.strip()
        )
    checks = raw.get("diagnostic_checks")
    checks = checks if isinstance(checks, dict) else {}
    warnings = checks.get("warnings")
    return {
        "fit_axes": tuple(axes),
        "fit_admissibility": tuple(dimensions),
        "fit_reliability": tuple(descriptions),
        "fit_warnings": tuple(item for item in warnings if isinstance(item, str))
        if isinstance(warnings, list)
        else (),
    }
