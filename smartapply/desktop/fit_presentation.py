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


def fit_summary(diagnostics, *, has_score: bool) -> dict:
    raw = diagnostics if isinstance(diagnostics, dict) else {}
    current = has_score and raw.get("policy_fingerprint") == current_policy_fingerprint()
    return {
        "fit_current": current,
        "fit_status_label": (
            "Évaluation actuelle"
            if current
            else "Évaluation historique · à réévaluer"
            if has_score
            else ""
        ),
    }


def fit_detail(diagnostics, *, match_reasons=()) -> dict:
    raw = diagnostics if isinstance(diagnostics, dict) else {}
    if raw.get("score_source") == "llm" or not raw.get("used_decisions"):
        return {"fit_axes": (), "fit_reliability": (), "fit_warnings": ()}
    decisions = raw.get("used_decisions")
    decisions = decisions if isinstance(decisions, dict) else {}
    levels = raw.get("used_levels")
    levels = levels if isinstance(levels, dict) else {}
    evaluation = raw.get("evaluation")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    annotations = evaluation.get("dimensions")
    annotations = annotations if isinstance(annotations, dict) else {}
    axes = []
    for axis, label in AXIS_LABELS.items():
        decision = decisions.get(axis)
        level = levels.get(axis)
        annotation = annotations.get(axis)
        annotation = annotation if isinstance(annotation, dict) else {}
        findings = annotation.get("findings")
        findings = findings if isinstance(findings, list) else []
        texts = []
        for item in findings:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            text = item["text"].strip()
            if not text:
                continue
            # Retain the saved explanation's evidence qualifiers when available.
            saved = next(
                (
                    reason
                    for reason in match_reasons
                    if reason == text or reason.endswith(": " + text)
                ),
                None,
            )
            support = item.get("support")
            prefix = {
                "declared_only": "Déclaré : ",
                "transferable": "Transférable : ",
                "partial_support": "Appui partiel, à confirmer : ",
                "not_documented": "Non documenté : ",
                "contradicted": "Écart établi : ",
            }.get(support if isinstance(support, str) else "", "")
            text = saved or prefix + text
            if text not in texts:
                texts.append(text)
        axes.append(
            {
                "axis": axis,
                "label": label,
                "decision": decision if isinstance(decision, str) else "",
                "decision_label": DECISION_LABELS.get(decision, "Indisponible")
                if isinstance(decision, str)
                else "Indisponible",
                "level": level if type(level) is int and 0 <= level <= 4 else None,
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
        "fit_reliability": tuple(descriptions),
        "fit_warnings": tuple(item for item in warnings if isinstance(item, str))
        if isinstance(warnings, list)
        else (),
    }
