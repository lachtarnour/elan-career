"""Top selection based on the persisted AI fit assessment."""

from __future__ import annotations

from sqlalchemy import select

from smartapply.database.models import Job, ShortlistOrigin
from smartapply.database.repository import set_shortlisted


class ShortlistMixin:
    @staticmethod
    def _replace_automatic_shortlist(session, candidates: list[Job]) -> list[Job]:
        """Replace automatic selections while preserving manual choices."""
        automatic_ids = {int(job.id) for job in candidates}
        previous_shortlisted = session.execute(
            select(Job).where(
                Job.shortlisted_at.is_not(None),
                Job.archived_at.is_(None),
                Job.shortlist_origin == ShortlistOrigin.AUTOMATIC,
            )
        ).scalars()
        for job in previous_shortlisted:
            if int(job.id) not in automatic_ids:
                set_shortlisted(session, job.id, selected=False)

        for job in candidates:
            set_shortlisted(
                session,
                job.id,
                selected=True,
                origin=ShortlistOrigin.AUTOMATIC,
            )
        return candidates
