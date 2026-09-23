"""Practical application-tracking helpers.

The project is first a job-search companion, so this module keeps the
human workflow visible: what state a candidature is in, and what should
happen next.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from smartapply.database.models import JobStatus
from smartapply.jobsearch.status import display_status

APPLICATION_STATUSES = [
    JobStatus.READY_FOR_FORM_SUBMISSION,
    JobStatus.SENT,
    JobStatus.INTERVIEW,
    JobStatus.REJECTED,
    JobStatus.ARCHIVED,
]


def _date_label(value: datetime | None, *, days: int) -> str:
    if value is None:
        return f"J+{days}"
    due = value + timedelta(days=days)
    return due.strftime("%d/%m/%Y")


def next_action_for(
    status: str,
    updated_at: datetime | None = None,
) -> str:
    """Return a short next-action hint for the application tracker."""
    status = display_status(status)
    if status == JobStatus.DUPLICATE_REVIEW:
        return "Vérifier l’offre ressemblante"
    if status == JobStatus.READY_FOR_FORM_SUBMISSION:
        return "Relire les documents de candidature"
    if status == JobStatus.SENT:
        return f"Relancer sans réponse le {_date_label(updated_at, days=7)}"
    if status == JobStatus.INTERVIEW:
        return "Préparer l’entretien"
    if status == JobStatus.REJECTED:
        return "Archiver la candidature"
    if status == JobStatus.ARCHIVED:
        return "Aucune action"
    return "Mettre à jour le suivi"
