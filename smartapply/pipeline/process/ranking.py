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
from smartapply.pipeline.reports import RankingReport
from smartapply.pipeline.snapshots import check_cancelled, job_is_active, job_snapshot_key


class RankingMixin:
    """Rank locally kept jobs and persist shortlist status."""

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
        with session_scope() as session:
            active_jobs = list(session.scalars(
                select(Job).where(
                    Job.archived_at.is_(None), Job.analyzed_at.is_(None),
                    or_(Job.duplicate_review_status.is_(None),
                        Job.duplicate_review_status != JobDuplicateStatus.PENDING),
                ).options(selectinload(Job.score), selectinload(Job.analysis))
            ))
            pending = [job for job in active_jobs if job.ranked_at is None]
            if job_ids is not None:
                selected_ids = set(job_ids)
                pending = [job for job in pending if job.id in selected_ids]
            snapshots = {int(job.id): job_snapshot_key(job) for job in pending}
            duplicate_ids = self._mark_duplicates(session, active_jobs)
            session.expunge_all()

        override_ids = set(local_filter_override_ids or [])
        filter_results = {
            int(job.id): self.filter.evaluate(job)
            for job in pending if job.id not in duplicate_ids and job.filtered_at is None
        }
        kept = [job for job in pending if job.id not in duplicate_ids and (
            job.filtered_at is not None or filter_results[job.id].kept or job.id in override_ids
        )]
        check_cancelled(stop_requested)
        ranked = self.scorer.rank(kept) if kept else []
        check_cancelled(stop_requested)
        with write_session_scope() as session:
            current_by_id = {int(job.id): job for job in session.scalars(
                select(Job).where(Job.id.in_(snapshots))
                .options(selectinload(Job.score), selectinload(Job.analysis))
            )}
            valid_ids = {
                job_id for job_id, job in current_by_id.items()
                if job_is_active(job) and job_snapshot_key(job) == snapshots[job_id]
            }
            for job_id, result in filter_results.items():
                if job_id in valid_ids:
                    self._persist_filter_result(
                        session, current_by_id[job_id], result,
                        force_keep=job_id in override_ids, update_pipeline_status=True,
                    )
            session.flush()
            for job_id in valid_ids & filter_results.keys():
                session.expire(current_by_id[job_id], ["score"])
            self._persist_scores(session, [
                (current_by_id[job.id], components) for job, components in ranked
                if job.id in valid_ids
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
            components = comp.to_dict()
            if previous_components.get("reasons"):
                components["reasons"] = previous_components["reasons"]
            if previous_components.get("filter_disposition"):
                components["filter_disposition"] = previous_components["filter_disposition"]
            set_score(
                session,
                job.id,
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
        return list(
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
