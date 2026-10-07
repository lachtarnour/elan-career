"""Resume the unfinished extraction/evaluation boundary in either execution mode."""

from sqlalchemy import select

from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchWave
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.llm import OfferAnalysis
from smartapply.llm.cache import make_cache_key
from smartapply.llm.prompts import job_analysis


def extraction_key(analyzer_input, model: str) -> str:
    return make_cache_key(
        model=model,
        system=job_analysis.build_system_prompt(),
        user=job_analysis.build_user_prompt_from_input(analyzer_input=analyzer_input),
        schema_name=OfferAnalysis.__name__,
        extra={"temperature": 0.2, "schema": OfferAnalysis.model_json_schema()},
    )


def save_extraction(session, job, key: str, offer: OfferAnalysis) -> None:
    checkpoint = session.get(AnalysisCheckpoint, job.id)
    if checkpoint is None:
        checkpoint = AnalysisCheckpoint(job_id=job.id)
        session.add(checkpoint)
    checkpoint.extraction_key = key
    checkpoint.offer_data = offer.model_dump(mode="json")


def load_extraction(session, job, key: str) -> OfferAnalysis | None:
    """Only an identical model, prompt, source input and schema may be resumed.

    Legacy Batch journals are recovered lazily for selected offers. Their
    persisted request key is required: rebuilding a past prompt with today's
    code would incorrectly accept an extraction made with an older contract.
    """
    checkpoint = session.get(AnalysisCheckpoint, job.id)
    if checkpoint is not None:
        if checkpoint.extraction_key == key:
            try:
                return OfferAnalysis.model_validate(checkpoint.offer_data)
            except ValueError:
                pass
        session.delete(checkpoint)
        session.flush()
    items = session.scalars(
        select(AnalysisBatchItem)
        .where(
            AnalysisBatchItem.job_id == job.id,
            AnalysisBatchItem.offer_data.is_not(None),
        )
        .order_by(AnalysisBatchItem.id.desc())
    )
    for item in items:
        requests = session.scalars(
            select(AnalysisBatchWave.requests).where(
                AnalysisBatchWave.run_id == item.run_id,
                AnalysisBatchWave.phase == "extraction",
            )
        )
        if not any(
            record.get("cache_key") == key and item.id in record.get("item_ids", [])
            for wave_requests in requests
            for record in wave_requests
        ):
            continue
        try:
            offer = OfferAnalysis.model_validate(item.offer_data)
        except ValueError:
            continue
        save_extraction(session, job, key, offer)
        return offer
    return None


def clear_extraction(session, job_id: int) -> None:
    checkpoint = session.get(AnalysisCheckpoint, job_id)
    if checkpoint is not None:
        session.delete(checkpoint)
