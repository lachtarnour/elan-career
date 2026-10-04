"""Semantic ranking phase."""

from __future__ import annotations

from sqlalchemy import or_, select

from smartapply.database import session_scope
from smartapply.database.models import (
    Application,
    Job,
    JobDuplicateStatus,
    JobScore,
    ShortlistOrigin,
)
from smartapply.database.repository import (
    list_pending_processing,
    mark_ranked,
    set_score,
    set_shortlisted,
)
from smartapply.pipeline.reports import RankingReport


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
        with session_scope() as s:
            active_jobs = list(list_pending_processing(s))
            rankable_jobs = [job for job in active_jobs if job.ranked_at is None]
            pending = list(rankable_jobs)
            if job_ids is not None:
                selected_ids = set(job_ids)
                pending = [job for job in pending if job.id in selected_ids]
            duplicate_ids: set[int] = set()
            ranked: list[tuple[Job, object]] = []
            if pending:
                override_ids = set(local_filter_override_ids or [])
                duplicate_ids = self._mark_duplicates(s, active_jobs)
                unique_jobs = [j for j in pending if j.id not in duplicate_ids]

                to_filter = [j for j in unique_jobs if j.filtered_at is None]
                already_kept = [j for j in unique_jobs if j.filtered_at is not None]
                newly_kept = self._apply_local_filter(
                    s,
                    to_filter,
                    override_ids=override_ids,
                )
                ranked = self.scorer.rank(already_kept + newly_kept)
                self._persist_scores(s, ranked)

            ranked_candidates = self._ranked_shortlist_candidates(
                s,
                unanalyzed_only=not persist_shortlist,
            )
            selection_candidates = (
                self._ranked_shortlist_candidates(s, analyzed_only=True)
                if persist_shortlist
                else ranked_candidates
            )
            selection_limit = min(
                top_k_ranked or self.settings.top_k_ranked,
                len(selection_candidates),
            )
            if persist_shortlist:
                selected = self._mixed_score_shortlist(selection_candidates, selection_limit)
                selected = self._replace_automatic_shortlist(s, selected)
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
