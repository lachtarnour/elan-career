"""Local deterministic filtering phase."""

from __future__ import annotations

from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import Job, JobStatus
from smartapply.database.repository import (
    mark_archived,
    mark_filtered,
    set_score,
    update_status,
)
from smartapply.filtering.types import FilterDisposition, FilterResult
from smartapply.pipeline.process.audit import _rejection_audit_components
from smartapply.pipeline.reports import LocalFilterReport


class LocalFilterMixin:
    """Apply deterministic filters and persist rejection audit data."""

    @staticmethod
    def _persist_filter_result(
        session,
        job: Job,
        result: FilterResult,
        *,
        force_keep: bool = False,
        update_pipeline_status: bool = False,
    ) -> bool:
        """Persist one filter decision and return whether the job was kept."""
        reasons = list(result.reasons)
        kept = result.kept or force_keep
        if force_keep and not result.kept:
            reasons.append("Manual override: kept by user")

        if kept:
            components = {
                "reasons": reasons,
                "filter_disposition": (
                    result.disposition.value if result.disposition is not None else "rejected"
                ),
            }
        else:
            components = _rejection_audit_components("local_filter", reasons)
            components["filter_disposition"] = "rejected"

        set_score(
            session,
            job.id,
            rule_based_score=result.score,
            components=components,
        )
        if kept:
            mark_filtered(session, job.id)
            if update_pipeline_status:
                update_status(session, job.id, JobStatus.FILTERED)
        else:
            mark_archived(session, job.id)
        return kept

    def _apply_local_filter(
        self,
        session,
        jobs: list[Job],
        *,
        override_ids: set[int] | None = None,
    ) -> list[Job]:
        kept: list[Job] = []
        override_ids = override_ids or set()
        for job in jobs:
            if self._persist_filter_result(
                session,
                job,
                self.filter.evaluate(job),
                force_keep=job.id in override_ids,
                update_pipeline_status=True,
            ):
                kept.append(job)
        return kept

    def filter_pending(self, *, job_ids: list[int] | None = None) -> LocalFilterReport:
        """Apply only deterministic local gates, without ranking or LLM calls.

        Used right after fetch in the workflow: internships, alternance and
        too-senior roles disappear before the user spends attention or LLM
        budget. The macOS search location remains authoritative. Kept jobs get ``filtered_at``
        set so a later ``process_pending`` correctly resumes from ranking
        instead of re-filtering or seeing nothing pending.
        """
        with session_scope() as s:
            # Review blocks ranking/AI, but must not block deterministic
            # rejection: both sides need a filter result before comparison.
            active_jobs = list(
                s.scalars(
                    select(Job).where(
                        Job.archived_at.is_(None),
                        Job.status != JobStatus.ARCHIVED,
                        Job.canonical_job_id.is_(None),
                        Job.analyzed_at.is_(None),
                    )
                )
            )
            pending = [j for j in active_jobs if j.filtered_at is None]
            if job_ids is not None:
                selected_ids = set(job_ids)
                pending = [job for job in pending if job.id in selected_ids]
            if not pending:
                return LocalFilterReport(0, 0, 0, 0, [], [])

            dedup_scope_by_id = {int(job.id): job for job in active_jobs}
            for job in pending:
                dedup_scope_by_id[int(job.id)] = job
            duplicate_ids = self._mark_duplicates(s, list(dedup_scope_by_id.values()))
            unique_jobs = [j for j in pending if j.id not in duplicate_ids]
            kept_ids: list[int] = []
            uncertain_ids: list[int] = []
            pending_ids = {int(job.id) for job in pending}
            rejected_ids: list[int] = [
                int(job_id) for job_id in duplicate_ids if int(job_id) in pending_ids
            ]

            for job in unique_jobs:
                res = self.filter.evaluate(job)
                if self._persist_filter_result(s, job, res):
                    kept_ids.append(job.id)
                    if res.disposition is FilterDisposition.UNCERTAIN:
                        uncertain_ids.append(job.id)
                else:
                    rejected_ids.append(job.id)

            return LocalFilterReport(
                total=len(pending),
                kept=len(kept_ids),
                rejected=len(rejected_ids),
                duplicates_removed=len(duplicate_ids),
                kept_ids=kept_ids,
                rejected_ids=rejected_ids,
                uncertain=len(uncertain_ids),
                uncertain_ids=uncertain_ids,
            )
