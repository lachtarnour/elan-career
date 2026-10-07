"""Keep denormalized progress and active filter routing consistent."""

from collections.abc import Mapping
from datetime import datetime, timezone

from smartapply.database.models import Job
from smartapply.jobsearch.offer_state import FILTER_HISTORY_KEY, offer_state

OFFER_STATE_VERSION = "offer-state-v1"


def retire_filter_routing(job: Job, *, score=None) -> bool:
    """Close active classification without interfering with an in-flight phase."""
    score = score if score is not None else job.score
    components = score.components if score is not None else None
    state = offer_state(job, components=components)
    if isinstance(components, Mapping) and "filter_disposition" in components and not state.filter_disposition:
        updated = dict(components)
        disposition = updated.pop("filter_disposition")
        updated[FILTER_HISTORY_KEY] = {
            "version": OFFER_STATE_VERSION,
            "disposition": disposition,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        score.components = updated
        return True
    return False


def synchronize_offer_state(job: Job) -> bool:
    """Repair persisted progress without rewriting paid results or documents."""
    changed = retire_filter_routing(job)
    state = offer_state(job)
    if (state.completed or state.workflow_status == "archived") and job.status != state.workflow_status:
        job.status = state.workflow_status
        changed = True
    if job.analysis is not None and job.analyzed_at is None:
        # The analysis creation date is evidence; generation alone is not.
        job.analyzed_at = job.analysis.created_at
        changed = True
    return changed
