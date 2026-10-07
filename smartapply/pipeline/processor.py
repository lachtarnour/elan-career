"""Deterministic preparation, exhaustive analysis and independent ranking."""

from __future__ import annotations

from collections.abc import Callable

from smartapply.config import get_settings
from smartapply.dedup import Deduplicator
from smartapply.filtering import DeterministicRejector
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
    """Analyze all deterministic survivors; ranking remains an optional operation."""

    def __init__(
        self,
        *,
        profile: Profile,
        deduplicator: Deduplicator,
        job_filter: DeterministicRejector,
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
        """Prepare and analyze the entire pending queue or explicit selection.

        ``top_k_analyze`` is retained for compatibility with older callers and
        deliberately does not truncate analysis. No embedding call is needed.
        """
        prepared = self.prepare_analysis(
            job_ids=job_ids,
            local_filter_override_ids=local_filter_override_ids,
            exclude_applications=job_ids is None,
        )
        candidates = self.analysis_candidates(
            prepared.kept_ids,
            exclude_applications=job_ids is None,
        )
        analyzed, analysis_errors = self._analyze_in_parallel(
            candidates, exclude_applications=job_ids is None,
        )

        return ProcessReport(
            total=prepared.total,
            kept_after_filter=prepared.kept,
            duplicates_removed=prepared.duplicates_removed,
            top_ranked=len(candidates),
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
