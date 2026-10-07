"""Semantic ranking phase."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import (
    Application,
    Job,
    JobDuplicateStatus,
    JobScore,
    ShortlistOrigin,
)
from smartapply.database.repository import (
    mark_ranked,
    set_score,
    set_shortlisted,
)
from smartapply.filtering.hard_reject.types import HardRejectDisposition
from smartapply.pipeline.process.analysis_admission import has_protected_workflow_history
from smartapply.pipeline.process.filter_state import (
    current_hard_reject_disposition,
    has_manual_keep_override,
    is_current_hard_reject_survivor,
)
from smartapply.pipeline.process.score_components import replace_ranking_components
from smartapply.pipeline.reports import RankingReport
from smartapply.pipeline.snapshots import check_cancelled, job_is_active, job_snapshot_key


class RankingMixin:
    """Rank locally kept jobs and persist shortlist status."""

    @staticmethod
    def _filter_disposition(job: Job) -> HardRejectDisposition | None:
        components = job.score.components if job.score is not None else None
        return current_hard_reject_disposition(components)

    @staticmethod
    def _has_manual_keep_override(job: Job) -> bool:
        components = job.score.components if job.score is not None else None
        return has_manual_keep_override(components)

    @staticmethod
    def _has_protected_filter_history(job: Job) -> bool:
        """Return whether filter repair must preserve the workflow record."""

        return has_protected_workflow_history(job)

    @staticmethod
    def _needs_ranking(job: Job) -> bool:
        """Return whether the persisted ranking contract is incomplete."""

        return (
            job.ranked_at is None
            or job.score is None
            or job.score.final_score is None
        )

    def rank_pending(
        self,
        top_k_ranked: int | None = None,
        *,
        job_ids: list[int] | None = None,
        local_filter_override_ids: list[int] | None = None,
        persist_shortlist: bool = True,
    ) -> RankingReport:
        """Run dedup, local filtering and ranking without calling the LLM.

        ``persist_shortlist`` is disabled by the analysis workflow. In that
        mode the method only returns the highest-scoring pending offers for
        analysis; it does not mark them as ready for document generation.
        """
        stop_requested = getattr(self, "stop_requested", None)
        check_cancelled(stop_requested)
        requested_ids = set(job_ids or [])
        requested_override_ids = set(local_filter_override_ids or [])
        with session_scope() as session:
            conditions = [
                Job.archived_at.is_(None),
                or_(
                    Job.duplicate_review_status.is_(None),
                    Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                ),
            ]
            if job_ids is None:
                # Routine ranking stays limited to the pending-analysis queue.
                # An analyzed legacy row is repaired only through an explicit
                # user-selected workflow, never as a startup or global sweep.
                conditions.append(Job.analyzed_at.is_(None))
            else:
                conditions.append(
                    or_(Job.analyzed_at.is_(None), Job.id.in_(requested_ids))
                )
            active_jobs = list(
                session.scalars(
                    select(Job)
                    .where(*conditions)
                    .options(
                        selectinload(Job.score),
                        selectinload(Job.analysis),
                        selectinload(Job.application),
                    )
                )
            )
            force_keep_ids = requested_override_ids | {
                int(job.id) for job in active_jobs
                if self._has_manual_keep_override(job)
            }
            pending = []
            for job in active_jobs:
                disposition = self._filter_disposition(job)
                rejected = disposition is HardRejectDisposition.REJECTED
                needs_filter = job.filtered_at is None or disposition is None
                if (
                    (not rejected and (needs_filter or self._needs_ranking(job)))
                    or (rejected and int(job.id) in force_keep_ids)
                    or (
                        rejected and needs_filter
                        and not self._has_protected_filter_history(job)
                    )
                ):
                    pending.append(job)
            if job_ids is not None:
                pending = [job for job in pending if job.id in requested_ids]
            snapshots = {int(job.id): job_snapshot_key(job) for job in pending}
            duplicate_ids = self._mark_duplicates(session, active_jobs)
            session.expunge_all()

        filter_results = {}
        kept = []
        for job in pending:
            job_id = int(job.id)
            if job_id in duplicate_ids:
                continue
            disposition = self._filter_disposition(job)
            needs_filter = (
                job.filtered_at is None
                or disposition is None
                or (
                    disposition is HardRejectDisposition.REJECTED
                    and job_id in force_keep_ids
                )
            )
            if needs_filter:
                result = self.filter.evaluate(job)
                filter_results[job_id] = result
                protected_complete_rank = (
                    self._has_protected_filter_history(job)
                    and not self._needs_ranking(job)
                )
                if (result.kept or job_id in force_keep_ids) and not protected_complete_rank:
                    kept.append(job)
            elif disposition is HardRejectDisposition.PENDING_RELEVANCE:
                kept.append(job)
        check_cancelled(stop_requested)
        ranked = self.scorer.rank(kept) if kept else []
        check_cancelled(stop_requested)
        with write_session_scope() as session:
            current_by_id = {int(job.id): job for job in session.scalars(
                select(Job).where(Job.id.in_(snapshots))
                .options(
                    selectinload(Job.score),
                    selectinload(Job.analysis),
                    selectinload(Job.application),
                )
            )}
            valid_ids = {
                job_id for job_id, job in current_by_id.items()
                if job_is_active(job) and job_snapshot_key(job) == snapshots[job_id]
            }
            for job_id, result in filter_results.items():
                if job_id in valid_ids:
                    job = current_by_id[job_id]
                    force_keep = (
                        job_id in requested_override_ids
                        or self._has_manual_keep_override(job)
                    )
                    protected_history = self._has_protected_filter_history(job)
                    if protected_history:
                        self._persist_filter_state_only(
                            session,
                            job,
                            result,
                            force_keep=force_keep,
                        )
                    else:
                        self._persist_filter_result(
                            session,
                            job,
                            result,
                            force_keep=force_keep,
                            update_pipeline_status=job.analyzed_at is None,
                        )
            session.flush()
            for job_id in valid_ids & filter_results.keys():
                session.expire(current_by_id[job_id], ["score"])
            ranking_eligible_ids = {
                job_id
                for job_id in valid_ids
                if is_current_hard_reject_survivor(
                    current_by_id[job_id].score.components
                    if current_by_id[job_id].score is not None
                    else None
                )
            }
            self._persist_scores(session, [
                (current_by_id[job.id], components) for job, components in ranked
                if job.id in ranking_eligible_ids
            ])
            ranked_candidates = self._ranked_shortlist_candidates(
                session, unanalyzed_only=not persist_shortlist,
            )
            if job_ids is not None and not persist_shortlist:
                requested_ids = set(job_ids)
                ranked_candidates = [job for job in ranked_candidates if job.id in requested_ids]
            selection_candidates = (
                self._ranked_shortlist_candidates(session, analyzed_only=True)
                if persist_shortlist else ranked_candidates
            )
            selection_limit = min(
                top_k_ranked or self.settings.top_k_ranked, len(selection_candidates),
            )
            if persist_shortlist:
                selected = self._mixed_score_shortlist(selection_candidates, selection_limit)
                selected = self._replace_automatic_shortlist(session, selected)
            else:
                selected = selection_candidates[:selection_limit]
            ranked_ids = [int(job.id) for job in ranked_candidates]
            shortlisted_ids = [int(job.id) for job in selected]

        return RankingReport(
            total=len(pending),
            kept_after_filter=len(ranked_ids),
            duplicates_removed=len(duplicate_ids),
            ranked=len(ranked_ids),
            shortlisted=len(shortlisted_ids),
            ranked_ids=ranked_ids,
            shortlisted_ids=shortlisted_ids,
        )

    def _persist_scores(
        self,
        session,
        ranked: list[tuple[Job, object]],
    ) -> None:
        for job, comp in ranked:
            previous_components = (
                dict(job.score.components) if job.score is not None and job.score.components else {}
            )
            components = replace_ranking_components(
                previous_components,
                comp.to_dict(),
            )
            set_score(
                session,
                job.id,
                rule_based_score=None,
                semantic_score=comp.semantic,
                skill_score=comp.skills,
                title_score=comp.title,
                seniority_score=comp.seniority,
                location_score=comp.location,
                domain_score=comp.domain,
                final_score=comp.final,
                components=components,
            )
            mark_ranked(session, job.id)

    @staticmethod
    def _ranked_shortlist_candidates(
        session,
        *,
        unanalyzed_only: bool = False,
        analyzed_only: bool = False,
    ) -> list[Job]:
        """Return active, scored offers eligible for the requested workflow."""
        if unanalyzed_only and analyzed_only:
            raise ValueError("unanalyzed_only and analyzed_only are mutually exclusive")
        conditions = [
            Job.archived_at.is_(None),
            or_(
                Job.duplicate_review_status.is_(None),
                Job.duplicate_review_status != JobDuplicateStatus.PENDING,
            ),
            Job.filtered_at.is_not(None),
            JobScore.final_score.is_not(None),
            Application.id.is_(None),
        ]
        if unanalyzed_only:
            conditions.append(Job.analyzed_at.is_(None))
        elif analyzed_only:
            conditions.append(Job.analyzed_at.is_not(None))
        candidates = list(
            session.execute(
                select(Job)
                .join(JobScore, JobScore.job_id == Job.id)
                .outerjoin(Application, Application.job_id == Job.id)
                .where(*conditions)
                .order_by(JobScore.final_score.desc(), Job.id.asc())
            )
            .scalars()
            .all()
        )
        return [
            job
            for job in candidates
            if is_current_hard_reject_survivor(
                job.score.components if job.score is not None else None
            )
        ]

    @staticmethod
    def _mixed_score_shortlist(candidates: list[Job], shortlist_n: int) -> list[Job]:
        """Build Top-K from the average of matching and LLM scores.

        Offers without an LLM score use their matching score until they are
        analyzed. This keeps the shortlist useful while the analysis queue is
        still in progress.
        """
        if shortlist_n <= 0:
            return []

        def matching_score(job: Job) -> float:
            value = job.score.final_score if job.score is not None else None
            return float(value) if value is not None else float("-inf")

        def llm_score(job: Job) -> float:
            analysis = job.analysis
            value = analysis.fit_score if analysis is not None else None
            return float(value) if value is not None else float("-inf")

        def combined_score(job: Job) -> float:
            matching = matching_score(job)
            llm = llm_score(job)
            if llm == float("-inf"):
                return matching
            return (matching + llm) / 2

        return sorted(
            candidates,
            key=lambda job: (-combined_score(job), int(job.id)),
        )[:shortlist_n]

    @staticmethod
    def _replace_automatic_shortlist(
        session,
        candidates: list[Job],
    ) -> list[Job]:
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
