"""Apply audited duplicate decisions without touching relevance or dossiers.

Historical cleanup releases only the relation; it never reopens archived jobs
or resets a shortlist. Strict copies may retain automatic filtering and scores;
manual choices, analyses and application dossiers protect a source alias.
The plan is pure, its complete source snapshot is checked under the writer
lock, and every mutation has an audit event in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from smartapply.database.models import DuplicateReviewEvent, Job
from smartapply.dedup.review_decisions import (
    AUTOMATIC_STATE_FIELDS,
    REVIEW_POLICY_VERSION,
    ReviewDecision,
    automatic_state,
    plan_pending_review_decisions,
)


class DuplicateReviewSnapshotChanged(RuntimeError):
    """An offer changed while its automatic relation decision was being planned."""


def _json_state(state: dict[str, Any]) -> dict[str, Any]:
    def serialize(value):
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            return value.astimezone(timezone.utc).isoformat()
        raise TypeError(f"Unsupported audit value: {type(value).__name__}")
    return json.loads(json.dumps(state, default=serialize))


def _history_snapshot(job: Job | None) -> str | None:
    """Protect dossiers, analyses, scores and generated documents during undo."""
    if job is None:
        return None
    def row_state(row):
        return ({column.name: getattr(row, column.name) for column in row.__table__.columns}
                if row is not None else None)
    state = _json_state({
        "offer": row_state(job),
        "application": row_state(job.application),
        "analysis": row_state(job.analysis),
        "score": row_state(job.score),
        "documents": sorted((row_state(document) for document in job.application.documents),
                            key=lambda value: value["id"]) if job.application else [],
    })
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def _planning_snapshot(job: Job) -> tuple[Any, ...]:
    """Include score decisions and fetch dates used by the automatic policy."""
    from smartapply.pipeline.snapshots import application_snapshot_key, job_snapshot_key

    score = ({column.name: getattr(job.score, column.name)
              for column in job.score.__table__.columns} if job.score is not None else None)
    score_digest = hashlib.sha256(
        json.dumps(_json_state({"score": score}), sort_keys=True).encode()
    ).hexdigest()
    return (
        job_snapshot_key(job), job.scraped_at,
        application_snapshot_key(job.application) if job.application else None,
        bool(job.analysis), score_digest,
    )


def record_automatic_review(
    session, job: Job, *, reference_id: int | None, action: str,
    reason: str, before: dict[str, Any],
) -> DuplicateReviewEvent:
    """Store the original and resulting relation atomically with its mutation."""
    session.flush()
    existing_reference = session.get(Job, reference_id) if reference_id is not None else None
    event = DuplicateReviewEvent(
        job_id=int(job.id), reference_id=int(existing_reference.id) if existing_reference else None,
        policy_version=REVIEW_POLICY_VERSION, action=action, reason=reason,
        before_state=_json_state(before), after_state=_json_state(automatic_state(job)),
        history_snapshot={"job": _history_snapshot(job),
                          "reference": _history_snapshot(existing_reference)},
    )
    session.add(event)
    return event


def apply_review_decisions(session, decisions: Sequence[ReviewDecision]) -> int:
    """Apply a previously validated plan to jobs attached to this transaction."""
    for decision in decisions:
        job = session.get(Job, decision.job_id)
        if job is None or automatic_state(job) != decision.before:
            raise DuplicateReviewSnapshotChanged("La relation a changé depuis sa vérification.")
        for field in AUTOMATIC_STATE_FIELDS:
            setattr(job, field, decision.after[field])
        record_automatic_review(
            session, job, reference_id=decision.reference_id, action=decision.action,
            reason=decision.reason, before=decision.before,
        )
    return len(decisions)


def reconcile_pending_reviews(*, stop_requested: Callable[[], bool] | None = None) -> int:
    """Reconcile an unchanged full snapshot, without running a provider."""
    from smartapply.concurrency import job_operation
    from smartapply.database import session_scope, write_session_scope

    if stop_requested and stop_requested():
        return 0
    with session_scope() as session:
        jobs = list(session.scalars(
            select(Job).options(joinedload(Job.application), joinedload(Job.analysis), joinedload(Job.score))
        ).unique())
        snapshots = {int(job.id): _planning_snapshot(job) for job in jobs}
        session.expunge_all()
    decisions = plan_pending_review_decisions(jobs)
    if not decisions or (stop_requested and stop_requested()):
        return 0
    with (job_operation((decision.job_id for decision in decisions),
                       operation="vérification automatique des doublons"),
          write_session_scope() as session):
        current_jobs = list(session.scalars(
            select(Job).options(joinedload(Job.application), joinedload(Job.analysis), joinedload(Job.score))
        ).unique())
        current = {int(job.id): _planning_snapshot(job) for job in current_jobs}
        if current != snapshots:
            raise DuplicateReviewSnapshotChanged("Les offres ont changé depuis leur vérification.")
        if stop_requested and stop_requested():
            return 0
        return apply_review_decisions(session, decisions)


def restore_automatic_review(session, event_id: int) -> bool:
    """Undo a relation decision only while its resulting state is unchanged."""
    event = session.get(DuplicateReviewEvent, event_id)
    if event is None or event.action == "undo":
        return False
    job = session.get(Job, event.job_id) if event.job_id is not None else None
    if job is None or _json_state(automatic_state(job)) != event.after_state:
        raise DuplicateReviewSnapshotChanged("La décision a été remplacée depuis sa création.")
    reference = session.get(Job, event.reference_id) if event.reference_id is not None else None
    if event.history_snapshot != {"job": _history_snapshot(job), "reference": _history_snapshot(reference)}:
        raise DuplicateReviewSnapshotChanged("L’historique a changé depuis cette décision.")
    before = automatic_state(job)
    for field in AUTOMATIC_STATE_FIELDS:
        value = event.before_state.get(field)
        if field == "archived_at" and value is not None:
            value = datetime.fromisoformat(value)
        setattr(job, field, value)
    record_automatic_review(session, job, reference_id=event.reference_id,
                            action="undo", reason=f"undo_event:{event.id}", before=before)
    return True
