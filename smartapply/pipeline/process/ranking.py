"""Top selection based on the persisted AI fit assessment."""

from __future__ import annotations

from contextlib import ExitStack

from sqlalchemy import select

from smartapply.concurrency import TaskConflictError, job_operation
from smartapply.database.models import Job, ShortlistOrigin
from smartapply.database.repository import set_shortlisted


class ShortlistMixin:
    @staticmethod
    def _manually_excluded_from_shortlist(job: Job) -> bool:
        components = job.score.components if job.score is not None else None
        decision = components.get("manual_shortlist") if isinstance(components, dict) else None
        return isinstance(decision, dict) and decision.get("selected") is False

    @staticmethod
    def _replace_automatic_shortlist(
        session,
        candidates: list[Job],
        *,
        reservations: ExitStack,
        skipped_busy_ids: set[int],
    ) -> list[Job]:
        """Keep occupied selections intact, with leases held through the caller's commit."""
        candidates = [
            job for job in candidates
            if not ShortlistMixin._manually_excluded_from_shortlist(job)
        ]
        automatic_ids = {int(job.id) for job in candidates}
        preserved_busy = []

        def reserve(job):
            try:
                reservations.enter_context(job_operation([job.id], operation="shortlist automatique"))
            except TaskConflictError:
                skipped_busy_ids.add(int(job.id))
                return False
            return True

        previous_shortlisted = session.execute(
            select(Job).where(
                Job.shortlisted_at.is_not(None),
                Job.archived_at.is_(None),
                Job.shortlist_origin == ShortlistOrigin.AUTOMATIC,
            )
        ).scalars()
        for job in previous_shortlisted:
            if int(job.id) not in automatic_ids:
                if reserve(job):
                    set_shortlisted(session, job.id, selected=False)
                else:
                    preserved_busy.append(job)

        shortlisted = []
        for job in candidates:
            if reserve(job):
                updated = set_shortlisted(
                    session,
                    job.id,
                    selected=True,
                    origin=ShortlistOrigin.AUTOMATIC,
                )
                if updated is not None:
                    shortlisted.append(updated)
        return [*shortlisted, *preserved_busy]
