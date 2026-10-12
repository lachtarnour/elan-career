"""Rank comparable global assessments by their IA score."""

from sqlalchemy import case, func

from smartapply.database.models import JobAnalysis


def current_policy_fingerprint() -> str:
    from smartapply.llm.fit_context import POLICY, fingerprint

    return fingerprint(POLICY)


def current_fit_expression():
    # SQL NULL means historical/unidentified, so comparisons always yield a boolean.
    return func.coalesce(
        func.json_extract(JobAnalysis.fit_diagnostics, "$.policy_fingerprint")
        == current_policy_fingerprint(),
        False,
    )


def comparable_fit_score():
    # Historical values stay visible, but never enter the current numeric ordering.
    return case((current_fit_expression(), JobAnalysis.fit_score), else_=None)


def fit_ranking_columns(*, ascending: bool = False):
    """Callers filter comparable candidates; only the IA score determines rank."""
    score = JobAnalysis.fit_score
    return ((score.asc() if ascending else score.desc()).nulls_last(),)


def fit_metadata_columns():
    """Only presentation metadata is materialized for offer lists."""
    return (
        func.json_extract(JobAnalysis.fit_diagnostics, "$.policy_fingerprint").label("fit_policy"),
    )
