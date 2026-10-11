"""Keep technical indices comparable and admissibility visible in SQL rankings."""

from sqlalchemy import case, func, or_

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


def admissibility_rank_expression():
    mandatory = func.json_extract(
        JobAnalysis.fit_diagnostics, "$.admissibility.mandatory_conditions.status"
    )
    preferences = func.json_extract(
        JobAnalysis.fit_diagnostics, "$.admissibility.candidate_preferences.status"
    )
    return case(
        (or_(mandatory == "incompatible", preferences == "incompatible"), 2),
        ((mandatory == "compatible") & (preferences == "compatible"), 0),
        else_=1,
    )


def comparable_fit_score():
    # Historical values stay visible, but never enter the current numeric ordering.
    return case((current_fit_expression(), JobAnalysis.fit_score), else_=None)


def fit_ranking_columns(*, ascending: bool = False):
    current = current_fit_expression()
    score = comparable_fit_score()
    return (
        case((current, 0), else_=1).asc(),
        case((current, admissibility_rank_expression()), else_=None).asc().nulls_last(),
        (score.asc() if ascending else score.desc()).nulls_last(),
    )


def fit_metadata_columns():
    """Only presentation metadata is materialized for offer lists."""
    return tuple(
        func.json_extract(JobAnalysis.fit_diagnostics, path).label(label)
        for path, label in (
            ("$.policy_fingerprint", "fit_policy"),
            ("$.admissibility.mandatory_conditions.status", "fit_mandatory"),
            ("$.admissibility.candidate_preferences.status", "fit_preferences"),
        )
    )
