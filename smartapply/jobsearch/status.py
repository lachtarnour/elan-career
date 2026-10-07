"""Status metadata shared by the macOS application and business services."""

from __future__ import annotations

from typing import Any

from smartapply.database.models import JobStatus

STATUS_FLOW: tuple[dict[str, Any], ...] = (
    {
        "status": JobStatus.DUPLICATE_REVIEW,
        "label": "Doublon à vérifier",
        "description": "Offre ressemblante : choisissez si elle est identique ou indépendante.",
        "group": "offers",
    },
    {
        "status": JobStatus.SCRAPED,
        "label": "Nouvelle",
        "description": (
            "Offre non rejetée par les règles déterministes, "
            "en attente de classification."
        ),
        "group": "offers",
    },
    {
        "status": JobStatus.ANALYZED,
        "label": "Analysée",
        "description": "Analyse détaillée terminée.",
        "group": "offers",
    },
    {
        "status": JobStatus.SHORTLISTED,
        "label": "Top sélection",
        "description": "Offre ajoutée à votre Top sélection.",
        "group": "offers",
    },
    {
        "status": JobStatus.READY_FOR_FORM_SUBMISSION,
        "label": "Prête",
        "description": "CV et lettre disponibles.",
        "group": "applications",
    },
    {
        "status": JobStatus.SENT,
        "label": "Envoyée",
        "description": "Candidature envoyée.",
        "group": "applications",
    },
    {
        "status": JobStatus.INTERVIEW,
        "label": "Entretien",
        "description": "Entretien obtenu.",
        "group": "applications",
    },
    {
        "status": JobStatus.REJECTED,
        "label": "Refusée",
        "description": "Réponse négative enregistrée.",
        "group": "applications",
    },
    {
        "status": JobStatus.ARCHIVED,
        "label": "Archivée",
        "description": "Offre retirée de la liste active.",
        "group": "applications",
    },
)

STATUS_LABELS = {row["status"]: row["label"] for row in STATUS_FLOW}
STATUS_LABELS[JobStatus.QUALITY_REJECTED] = STATUS_LABELS[JobStatus.READY_FOR_FORM_SUBMISSION]

# Legacy document checks are diagnostics, not a separate application queue.
# Keep reading their persisted value without rewriting historical records.
READY_APPLICATION_STATUSES = frozenset(
    {JobStatus.READY_FOR_FORM_SUBMISSION, JobStatus.QUALITY_REJECTED}
)


def display_status(status: str) -> str:
    """Map internal and legacy states to their visible workflow state."""
    if status == JobStatus.QUALITY_REJECTED:
        return JobStatus.READY_FOR_FORM_SUBMISSION
    if status == JobStatus.FILTERED:
        return JobStatus.SCRAPED
    return status


# New offers live in Analysis. The Offers filters start with analyzed offers;
# internal pre-analysis states remain available to the pipeline and its API.
OFFER_FILTER_STATUSES = (
    JobStatus.DUPLICATE_REVIEW,
    JobStatus.ANALYZED,
    JobStatus.SHORTLISTED,
    JobStatus.READY_FOR_FORM_SUBMISSION,
    JobStatus.SENT,
    JobStatus.ARCHIVED,
)


def status_label(status: str) -> str:
    """Return the concise product label for a persisted status."""
    status = display_status(status)
    return STATUS_LABELS.get(status, status.replace("_", " ").title())
