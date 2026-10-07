"""Score repository helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from smartapply.database.models import Application, Job, JobScore, JobStatus

_NUMERIC_SCORE_FIELDS = frozenset(
    {
        "rule_based_score",
        "semantic_score",
        "skill_score",
        "title_score",
        "seniority_score",
        "location_score",
        "domain_score",
        "final_score",
    }
)


def set_score(session: Session, job_id: int, **components: Any) -> JobScore:
    score = session.execute(select(JobScore).where(JobScore.job_id == job_id)).scalar_one_or_none()
    if score is None:
        components.setdefault("computed_at", datetime.now(timezone.utc))
        score = JobScore(job_id=job_id, **components)
        session.add(score)
    else:
        if _NUMERIC_SCORE_FIELDS.intersection(components):
            components.setdefault("computed_at", datetime.now(timezone.utc))
        for key, value in components.items():
            setattr(score, key, value)
    from smartapply.database.repository.offer_state import retire_filter_routing

    job = session.get(Job, job_id)
    if job is not None:
        retire_filter_routing(job, score=score)
    return score


def top_jobs_by_score(
    session: Session,
    k: int,
    *,
    unapplied_only: bool = False,
) -> Sequence[Job]:
    stmt = (
        select(Job)
        .join(JobScore)
        .where(
            JobScore.final_score.is_not(None),
            Job.archived_at.is_(None),
            Job.status != JobStatus.ARCHIVED,
        )
    )
    if unapplied_only:
        stmt = stmt.outerjoin(Application).where(Application.id.is_(None))
    stmt = stmt.order_by(JobScore.final_score.desc()).limit(k)
    return session.execute(stmt).scalars().all()
