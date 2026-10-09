"""Conservative, auditable mission gate before new offer extraction.

The model classifies the offer's dominant work with closed values. Python only
applies the two-value decision and protects explicit user actions and existing
work. Missing or uncertain classification continues through the pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from smartapply.database.models import Job
from smartapply.database.repository import mark_archived, set_score
from smartapply.filtering.manual_policy import has_manual_filter_bypass
from smartapply.llm.schemas import MissionScreen, OfferAnalysis
from smartapply.logging_setup import get_logger
from smartapply.pipeline.process.audit import _rejection_audit_components
from smartapply.pipeline.process.filter_state import has_manual_keep_override

SCREEN_REJECTION_REASON = "mission_clearly_outside"
SCREEN_REJECTION_STAGE = "extraction_screening"
logger = get_logger(__name__)


def extraction_offer_facts(offer: OfferAnalysis | Mapping[str, object]) -> OfferAnalysis:
    """Project the unchanged factual extraction fields for fit and documents."""

    raw = offer.model_dump() if isinstance(offer, OfferAnalysis) else offer
    return OfferAnalysis.model_validate(
        {key: raw[key] for key in OfferAnalysis.model_fields if key in raw}
    )


def _manual_override(components: Mapping[str, object]) -> bool:
    return has_manual_filter_bypass(components) or has_manual_keep_override(components)


def apply_mission_screen(session, job: Job, screen, *, screening_key=None) -> bool:
    """Apply the reversible gate to the independent mission verdict."""
    if screen is not None:
        try:
            screen = MissionScreen.model_validate(screen)
        except ValidationError:
            screen = None
    if screen is None:
        return False

    previous = dict(job.score.components) if job.score and job.score.components else {}
    decisive = (
        screen.mission_relation == "clearly_outside" and screen.mission_evidence == "sufficient"
    )
    protected = (
        job.analysis is not None or job.analyzed_at is not None or job.application is not None
    )
    manual = _manual_override(previous)
    archive = decisive and not manual and not protected

    if archive:
        decision = "archived"
    elif manual:
        decision = "evaluate_manual_override"
    elif protected:
        decision = "evaluate_existing_work"
    else:
        decision = "evaluate"

    components = {
        **previous,
        "extraction_screen": {
            "schema_version": 1,
            "mission_relation": screen.mission_relation,
            "mission_evidence": screen.mission_evidence,
            "decision": decision,
            **({"screening_key": screening_key} if screening_key else {}),
        },
    }
    if archive:
        components.update(
            _rejection_audit_components(SCREEN_REJECTION_STAGE, [SCREEN_REJECTION_REASON])
        )
    try:
        # Keep the retained extraction and caller's journal outside this
        # savepoint. A failed audit/archive must restore all routing/lifecycle
        # writes together and leave the offer available for fit evaluation.
        with session.begin_nested():
            set_score(session, job.id, components=components)
            if archive:
                mark_archived(session, job.id, origin="mission_screen")
    except Exception as exc:
        logger.warning(
            "Extraction screening write failed for job %s; evaluating retained facts: %s",
            job.id,
            exc,
        )
        return False
    return archive
