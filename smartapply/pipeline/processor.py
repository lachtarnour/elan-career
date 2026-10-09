"""Deterministic preparation, exhaustive analysis and Top selection."""

from __future__ import annotations

from collections.abc import Callable

from smartapply.config import get_settings
from smartapply.filtering import DeterministicRejector
from smartapply.llm import LLMProvider
from smartapply.pipeline.process import (
    AnalysisMixin,
    LocalFilterMixin,
    ShortlistMixin,
    _is_anonymous_company,
    _should_replace_job_company,
    _should_replace_job_location,
)
from smartapply.pipeline.reports import ProcessReport
from smartapply.profile import Profile


class Processor(AnalysisMixin, LocalFilterMixin, ShortlistMixin):
    """Analyze all deterministic survivors and maintain the Top selection."""

    def __init__(
        self,
        *,
        profile: Profile,
        job_filter: DeterministicRejector,
        llm: LLMProvider,
        stop_requested: Callable[[], bool] | None = None,
        use_global_profile: bool = False,
    ):
        self.stop_requested = stop_requested
        self.profile = profile
        self._uses_global_profile = use_global_profile
        self.filter = job_filter
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
        deliberately does not truncate analysis.
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
            candidates,
            exclude_applications=job_ids is None,
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
