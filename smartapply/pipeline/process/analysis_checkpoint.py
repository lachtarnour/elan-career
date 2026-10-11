"""Resume the unfinished extraction/evaluation boundary in either execution mode."""

from sqlalchemy import select

from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchWave
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.llm import OfferAnalysis, OfferExtractionOutput
from smartapply.llm.cache import make_cache_key
from smartapply.llm.prompts import job_analysis
from smartapply.llm.request_options import request_cache_extra


def extraction_key(
    analyzer_input, model: str, *, provider: str = "openai", effort: str | None = None
) -> str:
    return make_cache_key(
        provider=provider,
        model=model,
        system=job_analysis.build_system_prompt(),
        user=job_analysis.build_user_prompt_from_input(analyzer_input=analyzer_input),
        schema_name=OfferExtractionOutput.__name__,
        extra=request_cache_extra(OfferExtractionOutput, 0.2, effort),
    )


def read_extraction(data):
    if not isinstance(data, dict):
        raise ValueError("Missing extraction facts")
    return OfferExtractionOutput.model_validate(data)


def save_extraction(session, job, key: str, offer: OfferAnalysis) -> None:
    checkpoint = session.get(AnalysisCheckpoint, job.id)
    if checkpoint is None:
        checkpoint = AnalysisCheckpoint(job_id=job.id)
        session.add(checkpoint)
    checkpoint.extraction_key = key
    checkpoint.offer_data = offer.model_dump(mode="json")


def load_extraction(session, job, key: str) -> OfferAnalysis | None:
    """Only an identical model, prompt, source input and schema may be resumed.

    Completed Batch outputs may be reused when their recorded request has
    the exact same key as this extraction.
    """
    checkpoint = session.get(AnalysisCheckpoint, job.id)
    if checkpoint is not None and checkpoint.extraction_key == key:
        try:
            return read_extraction(checkpoint.offer_data)
        except ValueError:
            pass
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
            offer = read_extraction(item.offer_data)
        except ValueError:
            continue
        save_extraction(session, job, key, offer)
        return offer
    return None


def clear_extraction(session, job_id: int) -> None:
    checkpoint = session.get(AnalysisCheckpoint, job_id)
    if checkpoint is not None:
        session.delete(checkpoint)
