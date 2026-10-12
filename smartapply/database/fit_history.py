"""Lossless fit history and local restoration, independent of scoring policy."""

import copy
from datetime import datetime

from sqlalchemy.orm import Session

from smartapply.database.models import JobAnalysis, JobFitHistory
from smartapply.llm.fit_context import fingerprint


def archive_fit(session: Session, analysis: JobAnalysis) -> JobFitHistory:
    snapshot = {
        column.name: copy.deepcopy(getattr(analysis, column.name))
        for column in JobAnalysis.__table__.columns
        if column.name not in {"id", "job_id"}
    }
    if isinstance(snapshot["created_at"], datetime):
        snapshot["created_at"] = snapshot["created_at"].isoformat()
    saved = JobFitHistory(
        job_id=analysis.job_id, snapshot=snapshot, fingerprint=fingerprint(snapshot)
    )
    session.add(saved)
    return saved


def restore_fit(session: Session, job_id: int, history_id: int) -> JobAnalysis:
    """Restore a complete saved assessment and archive the displaced assessment."""
    from smartapply.database.repository.analyses import set_analysis

    saved = session.get(JobFitHistory, history_id)
    if saved is None or saved.job_id != job_id:
        raise ValueError("Historique absent ou associé à une autre offre.")
    if fingerprint(saved.snapshot) != saved.fingerprint:
        raise ValueError("L'intégrité de l'évaluation sauvegardée n'est pas valide.")
    fields = copy.deepcopy(saved.snapshot)
    if isinstance(fields.get("created_at"), str):
        fields["created_at"] = datetime.fromisoformat(fields["created_at"])
    return set_analysis(session, job_id, **fields)
