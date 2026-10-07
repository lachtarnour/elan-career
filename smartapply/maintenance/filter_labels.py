"""Complete missing two-state labels without changing workflow history.

Already labelled offers retain their decision, including historical rejections.
Only absent or obsolete labels are evaluated by the canonical local rejector.
The repair owns filter metadata alone: scores, dates, dossiers and duplicate
relations remain untouched.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, contains_eager

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Job, JobScore
from smartapply.filtering.hard_reject.types import HardRejectDisposition, HardRejectResult
from smartapply.jobsearch.offer_state import completed_offer_expression, offer_state
from smartapply.maintenance.hard_reject_backfill import build_rejector
from smartapply.pipeline.process.filter_state import has_manual_keep_override
from smartapply.pipeline.process.local_filtering import LocalFilterMixin
from smartapply.pipeline.process.score_components import (
    FILTER_COMPONENT_KEYS,
    replace_filter_state_components,
)

FILTER_LABELS_VERSION = "filter-labels-v2"
_VALID_LABELS = frozenset(item.value for item in HardRejectDisposition)
_REJECTION_KEYS = ("rejection_stage", "rejection_reasons", "rejection_summary")


class FilterLabelSnapshotChanged(RuntimeError):
    """Source content or persisted scores changed while labels were evaluated."""


def needs_filter_label(components: object) -> bool:
    """Proof refresh is separate; every existing two-state label is preserved."""
    return not (
        isinstance(components, Mapping)
        and isinstance(components.get("filter_disposition"), str)
        and components.get("filter_disposition") in _VALID_LABELS
    )


def _manual_keep(components: Mapping[str, object]) -> bool:
    if (
        has_manual_keep_override(components)
        or components.get("manual_keep") is True
        or components.get("manual_rescue") is True
    ):
        return True
    rescue = components.get("manual_rescue")
    # Current rescue_archived_job stores a dated, versioned user decision.
    return (
        isinstance(rescue, Mapping)
        and rescue.get("schema_version") in (1, 2, 3)
        and isinstance(rescue.get("rescued_at"), str)
        and bool(rescue["rescued_at"].strip())
    )


def persist_filter_label(
    session: Session,
    job: Job,
    result: HardRejectResult,
    *,
    migrate: bool = False,
) -> bool:
    """Write a missing label and its proof; return whether metadata changed.

    Numeric scores and their computation date are preserved. A newly required
    score row contains metadata only, with every numeric score left NULL.
    """
    previous_raw = job.score.components if job.score is not None else None
    state = offer_state(job)
    if state.completed or (state.workflow_status == "archived" and not state.filter_disposition):
        return False
    if not needs_filter_label(previous_raw):
        return False
    previous = dict(previous_raw) if isinstance(previous_raw, Mapping) else {}
    _, decision = LocalFilterMixin._filter_decision_components(
        result, force_keep=_manual_keep(previous),
    )
    components = replace_filter_state_components(previous, decision)
    if previous.get("rejection_stage") not in (None, "local_filter"):
        # Analysis, manual archiving and other stages own their rejection reason.
        for key in _REJECTION_KEYS:
            components.pop(key, None)
            if key in previous:
                components[key] = previous[key]
    if migrate:
        components["filter_label_completion"] = {
            "version": FILTER_LABELS_VERSION,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "previous_filter_components": {
                key: value for key, value in previous.items() if key in FILTER_COMPONENT_KEYS
            },
        }
        if previous_raw is not None and not isinstance(previous_raw, Mapping):
            components["filter_label_completion"]["previous_components"] = previous_raw
    if job.score is None:
        job.score = JobScore(components=components)
        session.add(job.score)
    else:
        job.score.components = components
    return True


def _snapshot(job: Job) -> str:
    def row_state(row):
        return (
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            if row is not None else None
        )
    encoded = json.dumps(
        {"job": row_state(job), "score": row_state(job.score)},
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _job_query():
    # The outer join includes offers that have never received a JobScore row.
    return select(Job).outerjoin(Job.score).options(contains_eager(Job.score))


def complete_missing_filter_labels(
    *, stop_requested: Callable[[], bool] | None = None,
) -> int:
    """Evaluate missing labels outside the writer, then apply an unchanged batch."""
    def stopped() -> bool:
        return stop_requested is not None and stop_requested()

    if stopped():
        return 0
    disposition = JobScore.components["filter_disposition"].as_string()
    with session_scope() as session:
        jobs = list(session.scalars(_job_query().where(
            ~completed_offer_expression(),
            Job.archived_at.is_(None), Job.status != "archived",
            or_(JobScore.id.is_(None), disposition.is_(None), disposition.not_in(_VALID_LABELS)),
        )))
        jobs = [job for job in jobs if needs_filter_label(
            job.score.components if job.score is not None else None
        )]
        snapshots = {int(job.id): _snapshot(job) for job in jobs}
        session.expunge_all()
    if not jobs or stopped():
        return 0
    rejector = build_rejector()
    results: dict[int, HardRejectResult] = {}
    for job in jobs:
        if stopped():
            return 0
        results[int(job.id)] = rejector.evaluate(job)
    with write_session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            # Reserve this short transaction before reading its authoritative state.
            session.execute(text("BEGIN IMMEDIATE"))
        current = {int(job.id): job for job in session.scalars(
            _job_query().where(Job.id.in_(snapshots))
        )}
        if {job_id: _snapshot(job) for job_id, job in current.items()} != snapshots:
            raise FilterLabelSnapshotChanged("Les offres ou leurs scores ont changé pendant le filtrage.")
        if stopped():
            return 0
        return sum(persist_filter_label(session, current[job_id], result, migrate=True)
                   for job_id, result in results.items())


__all__ = [
    "FILTER_LABELS_VERSION",
    "FilterLabelSnapshotChanged",
    "complete_missing_filter_labels",
    "needs_filter_label",
    "persist_filter_label",
]
