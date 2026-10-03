"""Phase 2 — dedup + local filter + semantic ranking + LLM analysis."""

from __future__ import annotations

from collections.abc import Callable

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.dedup import Deduplicator
from smartapply.filtering import JobFilter
from smartapply.llm import LLMProvider
from smartapply.pipeline.process import (
    AnalysisMixin,
    DeduplicationMixin,
    LocalFilterMixin,
    RankingMixin,
    _is_anonymous_company,
    _should_replace_job_company,
    _should_replace_job_location,
)
from smartapply.pipeline.reports import ProcessReport
from smartapply.profile import Profile
from smartapply.ranking import JobScorer


class Processor(AnalysisMixin, LocalFilterMixin, RankingMixin, DeduplicationMixin):
    """Run dedup → filter → semantic ranking → LLM analysis on top-K."""

    def __init__(
        self,
        *,
        profile: Profile,
        deduplicator: Deduplicator,
        job_filter: JobFilter,
        scorer: JobScorer,
        llm: LLMProvider,
        stop_requested: Callable[[], bool] | None = None,
    ):
        self.stop_requested = stop_requested
        self.profile = profile
        self.deduplicator = deduplicator
        self.filter = job_filter
        self.scorer = scorer
        self.llm = llm
        self.settings = get_settings()

    def process_pending(
        self,
        top_k_analyze: int | None = None,
        *,
        job_ids: list[int] | None = None,
        local_filter_override_ids: list[int] | None = None,
    ) -> ProcessReport:
        """Refresh the persistent Top selection, then analyze its pending offers."""
        ranking = self.rank_pending(
            top_k_ranked=top_k_analyze,
            job_ids=job_ids,
            local_filter_override_ids=local_filter_override_ids,
            persist_shortlist=False,
        )
        with session_scope() as s:
            analysis_candidates = list(
                s.query(Job)
                .filter(
                    Job.id.in_(ranking.shortlisted_ids),
                    Job.archived_at.is_(None),
                )
                .all()
            )

        to_analyze = [job for job in analysis_candidates if job.analyzed_at is None]
        analyzed, analysis_errors = self._analyze_in_parallel(to_analyze)

        return ProcessReport(
            total=ranking.total,
            kept_after_filter=ranking.kept_after_filter,
            duplicates_removed=ranking.duplicates_removed,
            top_ranked=len(analysis_candidates),
            analyzed=analyzed,
            analysis_errors=analysis_errors,
            cancelled=bool(self.stop_requested and self.stop_requested()),
        )


__all__ = [
    "Processor",
    "_is_anonymous_company",
    "_should_replace_job_company",
    "_should_replace_job_location",
]
