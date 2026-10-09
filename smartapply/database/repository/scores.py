"""Score repository helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from smartapply.database.models import Job, JobScore


def set_score(session: Session, job_id: int, **components: Any) -> JobScore:
    score = session.execute(select(JobScore).where(JobScore.job_id == job_id)).scalar_one_or_none()
    if score is None:
        components.setdefault("computed_at", datetime.now(timezone.utc))
        score = JobScore(job_id=job_id, **components)
        session.add(score)
    else:
        for key, value in components.items():
            setattr(score, key, value)
    from smartapply.database.repository.offer_state import retire_filter_routing

    job = session.get(Job, job_id)
    if job is not None:
        retire_filter_routing(job, score=score)
    return score
