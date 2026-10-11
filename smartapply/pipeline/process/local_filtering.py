"""Local deterministic filtering phase."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Job, JobStatus
from smartapply.database.repository import (
    mark_archived,
    mark_filtered,
    set_score,
    update_status,
)
from smartapply.filtering.hard_reject.types import (
    HardRejectDisposition,
    HardRejectResult,
)
from smartapply.filtering.manual_policy import (
    evaluate_or_bypass,
    has_manual_filter_bypass,
    manual_filter_bypass_result,
)
from smartapply.pipeline.process.audit import _rejection_audit_components
from smartapply.pipeline.process.filter_state import (
    MANUAL_KEEP_OVERRIDE_REASON,
    has_manual_keep_override,
)
from smartapply.pipeline.process.score_components import (
    replace_filter_components,
    replace_filter_state_components,
)
from smartapply.pipeline.reports import LocalFilterReport
from smartapply.pipeline.snapshots import check_cancelled, job_snapshot_key


class LocalFilterMixin:
    """Apply deterministic rejection gates and persist their evidence."""

    @staticmethod
    def _filter_decision_components(
        result: HardRejectResult,
        *,
        force_keep: bool = False,
    ) -> tuple[bool, dict[str, object]]:
        reasons = list(result.reasons)
        kept = result.kept or force_keep
        if force_keep and not result.kept:
            reasons.append(MANUAL_KEEP_OVERRIDE_REASON)

        if kept:
            disposition = (
                HardRejectDisposition.PENDING_RELEVANCE
                if force_keep
                else result.disposition
            )
            components = {
                "reasons": reasons,
                "filter_disposition": disposition.value,
                "hard_reject": result.audit_payload(),
            }
        else:
            components = _rejection_audit_components(
                "local_filter",
                result.blocking_reasons,
            )
            # Preserve diagnostics for investigation without presenting clear
            # or unresolved gates as causes of the rejection.
            components["reasons"] = reasons
            components["filter_disposition"] = "rejected"
            components["hard_reject"] = result.audit_payload()
        return kept, components

    @classmethod
    def _persist_filter_state_only(
        cls,
        session,
        job: Job,
        result: HardRejectResult,
        *,
        force_keep: bool = False,
    ) -> bool:
        """Repair filter metadata without rewriting protected workflow history."""

        previous_components = (
            dict(job.score.components) if job.score is not None and job.score.components else {}
        )
        force_keep = force_keep or has_manual_keep_override(previous_components)
        if has_manual_filter_bypass(previous_components):
            result = manual_filter_bypass_result()
        kept, current_components = cls._filter_decision_components(
            result,
            force_keep=force_keep,
        )
        set_score(
            session,
            job.id,
            components=replace_filter_state_components(
                previous_components,
                current_components,
            ),
        )
        if kept and job.filtered_at is None:
            mark_filtered(session, job.id)
        return kept

    @classmethod
    def _persist_filter_result(
        cls,
        session,
        job: Job,
        result: HardRejectResult,
        *,
        force_keep: bool = False,
        update_pipeline_status: bool = False,
    ) -> bool:
        """Persist one filter decision and return whether the job was kept."""

        previous_components = (
            dict(job.score.components) if job.score is not None and job.score.components else {}
        )
        force_keep = force_keep or has_manual_keep_override(previous_components)
        if has_manual_filter_bypass(previous_components):
            # A keep choice recorded during evaluation supersedes that stale
            # automatic result as well as future filtering passes.
            result = manual_filter_bypass_result()
        kept, current_components = cls._filter_decision_components(
            result,
            force_keep=force_keep,
        )

        components = replace_filter_components(previous_components, current_components)
        # Filtering invalidates any previous Top selection.
        job.ranked_at = None
        job.shortlisted_at = None
        job.shortlist_origin = None
        set_score(
            session,
            job.id,
            components=components,
        )
        if kept:
            mark_filtered(session, job.id)
            if update_pipeline_status:
                update_status(session, job.id, JobStatus.FILTERED)
        else:
            mark_archived(session, job.id, origin="automatic_filter")
        return kept

    def filter_pending(
        self, *, job_ids: list[int] | None = None, cancellable: bool = True,
    ) -> LocalFilterReport:
        """Apply only deterministic local gates, without ranking or LLM calls.

        Used right after fetch in the workflow: internships, alternance and
        too-senior roles disappear before the user spends attention or LLM
        budget. The macOS search location remains authoritative. Kept jobs get ``filtered_at``
        set so a later ``process_pending`` correctly resumes from ranking
        instead of re-filtering or seeing nothing pending.
        """
        # A completed collection must finish its local bookkeeping even if
        # the network worker was stopped. This phase never calls a provider.
        stop_requested = getattr(self, "stop_requested", None) if cancellable else None
        check_cancelled(stop_requested)
        with session_scope() as session:
            pending = list(session.scalars(select(Job).where(
                Job.archived_at.is_(None), Job.status != JobStatus.ARCHIVED,
                Job.canonical_job_id.is_(None), Job.analyzed_at.is_(None),
                Job.filtered_at.is_(None),
            ).options(selectinload(Job.score))))
            if job_ids is not None:
                selected_ids = set(job_ids)
                pending = [job for job in pending if job.id in selected_ids]
            snapshots = {int(job.id): job_snapshot_key(job) for job in pending}
            session.expunge_all()
        results = []
        for job in pending:
            check_cancelled(stop_requested)
            results.append((int(job.id), evaluate_or_bypass(self.filter, job)))
        kept_ids: list[int] = []
        pending_relevance_ids: list[int] = []
        rejected_ids: list[int] = []
        with write_session_scope() as session:
            current_by_id = {int(job.id): job for job in session.scalars(
                select(Job).where(Job.id.in_(snapshots))
            )}
            for job_id, result in results:
                job = current_by_id.get(job_id)
                if job is None or job_snapshot_key(job) != snapshots[job_id]:
                    continue
                if self._persist_filter_result(session, job, result):
                    kept_ids.append(job_id)
                    if result.disposition is HardRejectDisposition.PENDING_RELEVANCE:
                        pending_relevance_ids.append(job_id)
                else:
                    rejected_ids.append(job_id)
        return LocalFilterReport(
            total=len(pending), kept=len(kept_ids), rejected=len(rejected_ids),
            duplicates_removed=0, kept_ids=kept_ids, rejected_ids=rejected_ids,
            pending_relevance=len(pending_relevance_ids),
            pending_relevance_ids=pending_relevance_ids,
        )
