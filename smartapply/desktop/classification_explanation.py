"""Explain the persisted filter decision without evaluating the offer again."""

from __future__ import annotations

from typing import Any

from smartapply.jobsearch.archive_reasons import archive_reason_labels

_PENDING_SUMMARY = (
    "Aucune incompatibilité explicite n’a été prouvée. La pertinence reste à évaluer."
)
_LEGACY_AWAITING_EVALUATION_SUMMARY = (
    "Ancienne classification conservée dans l’historique ; l’offre attend "
    "l’évaluation du nouveau filtre de rejet, puis de sa pertinence."
)
_CHECK_REASONS = {
    "language": "Langue de l’offre à vérifier.",
    "contract": "Type de contrat à vérifier.",
    "experience": "Exigence d’expérience à vérifier.",
    "qualification": "Diplôme obligatoire à vérifier.",
    "seniority": "Niveau de responsabilité à vérifier.",
    "role": "Nature du poste à vérifier.",
}
_GATE_NAMES = {
    "language": "langue",
    "contract": "contrat",
    "experience": "expérience",
    "qualification": "diplôme",
    "seniority": "responsabilités",
    "role": "poste",
}


def classification_explanation(components: Any) -> tuple[str, tuple[str, ...]]:
    """Return a short summary and readable reasons from stored evidence only.

    Diagnostics and collection context never become rejection causes. Legacy
    relevance labels are compatibility states awaiting the current rejection
    and relevance evaluations. Migration snapshots are never current proof.
    """
    if not isinstance(components, dict):
        return "", ()
    disposition = components.get("filter_disposition")
    hard_reject = components.get("hard_reject")
    gates = _gates(hard_reject)
    if disposition == "pending_relevance":
        return _pending_explanation(components, gates)
    if disposition == "rejected":
        if isinstance(hard_reject, dict):
            return _hard_rejection_explanation(gates)
        return _legacy_rejection_explanation(components)
    if disposition in {"relevant", "uncertain"}:
        return _LEGACY_AWAITING_EVALUATION_SUMMARY, ()
    return "", ()


def _gates(hard_reject: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(hard_reject, dict):
        return ()
    values = hard_reject.get("gates")
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(value for value in values if isinstance(value, dict))


def _strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(value.strip() for value in values if isinstance(value, str) and value.strip())


def _pending_explanation(
    components: dict[str, Any],
    gates: tuple[dict[str, Any], ...],
) -> tuple[str, tuple[str, ...]]:
    reasons = tuple(
        dict.fromkeys(
            _CHECK_REASONS[gate["gate"]]
            for gate in gates
            if gate.get("status") == "unresolved"
            and isinstance(gate.get("gate"), str)
            and gate["gate"] in _CHECK_REASONS
        )
    )
    if any(reason.startswith("Manual override:") for reason in _strings(components.get("reasons"))):
        return "Offre conservée par décision manuelle ; la pertinence reste à évaluer.", reasons
    if any(gate.get("status") == "blocked" for gate in gates):
        return "Classement en attente ; les décisions enregistrées sont contradictoires.", reasons
    return _PENDING_SUMMARY, reasons


def _hard_rejection_explanation(
    gates: tuple[dict[str, Any], ...],
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    for gate in gates:
        if gate.get("status") != "blocked":
            continue
        labels = archive_reason_labels(_strings(gate.get("reason_codes")))
        reasons.extend(labels)
        # Quotes support an actual blocker; clear and unresolved evidence
        # cannot accidentally appear as an explanation for the rejection.
        if labels:
            reasons.extend(_evidence_excerpts(gate))
    if reasons:
        return "Au moins un critère de rejet est établi.", tuple(dict.fromkeys(reasons))
    return "Offre rejetée ; motif détaillé indisponible dans les données enregistrées.", ()


def _evidence_excerpts(gate: dict[str, Any]) -> tuple[str, ...]:
    values = gate.get("evidence")
    if not isinstance(values, (list, tuple)):
        return ()
    excerpts = []
    for evidence in values:
        if not isinstance(evidence, dict) or not isinstance(evidence.get("quote"), str):
            continue
        quote = " ".join(evidence["quote"].split())
        if not quote:
            continue
        if len(quote) > 180:
            quote = quote[:177].rstrip() + "…"
        gate_name = gate.get("gate")
        domain = _GATE_NAMES.get(gate_name, "critère") if isinstance(gate_name, str) else "critère"
        excerpts.append(f"Extrait — {domain} : « {quote} »")
        if len(excerpts) == 2:
            break
    return tuple(dict.fromkeys(excerpts))


def _legacy_rejection_explanation(components: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    values = _strings(components.get("rejection_reasons")) or _strings(components.get("reasons"))
    values = tuple(
        value
        for value in values
        if "conflict" not in value.partition(":")[0]
        and not value.partition(":")[0].endswith("_veto")
        and not value.startswith("Manual override:")
    )
    stage = components.get("rejection_stage")
    labels = archive_reason_labels(values, stage=stage if isinstance(stage, str) else None)
    return "Décision de rejet historique enregistrée.", labels


__all__ = ["classification_explanation"]
