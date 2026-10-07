"""Permanent removal of an offer's persisted working data.

Only confirmed copies belong to the deletion. Pending duplicate candidates
remain independent offers and lose their reference to the removed group.
The caller owns the transaction and the document publication locks.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from smartapply.database.analysis_batches import (
    ACTIVE_ANALYSIS_RUN_STATES,
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.models import (
    DuplicateReviewEvent,
    EmbeddingCache,
    Job,
    JobDuplicateStatus,
    LLMCache,
    LLMUsage,
)


def confirmed_offer_ids(session: Session, job_id: int) -> set[int]:
    members = {job_id}
    while True:
        rows = session.execute(
            select(Job.id, Job.canonical_job_id).where(
                or_(
                    Job.id.in_(members),
                    Job.canonical_job_id.in_(members),
                )
            )
        ).all()
        expanded = members | {value for row in rows for value in row if value is not None}
        if expanded == members:
            return members
        members = expanded


def assert_no_active_batch(session: Session, job_ids: set[int]) -> None:
    active = session.scalar(
        select(AnalysisBatchItem.id)
        .join(AnalysisBatchRun)
        .where(
            AnalysisBatchItem.job_id.in_(job_ids),
            AnalysisBatchRun.state.in_(ACTIVE_ANALYSIS_RUN_STATES),
        )
        .limit(1)
    )
    if active is not None:
        raise ValueError(
            "Annulez le batch de cette offre et attendez sa fin avant de la supprimer."
        )


def _json_fragments(value):
    """Legacy caches lack ownership; compare structured persisted results."""
    if isinstance(value, dict) and value:
        yield json.dumps(value, sort_keys=True, ensure_ascii=False)
        for child in value.values():
            yield from _json_fragments(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_fragments(child)


def _cache_evidence(session: Session, jobs: list[Job]) -> tuple[set[str], set[str]]:
    ids = {job.id for job in jobs}
    keys = set(
        session.scalars(
            select(LLMUsage.cache_key).where(
                LLMUsage.job_id.in_(ids),
                LLMUsage.cache_key.is_not(None),
            )
        )
    )
    keys.update(
        session.scalars(
            select(AnalysisCheckpoint.extraction_key).where(
                AnalysisCheckpoint.job_id.in_(ids),
            )
        )
    )
    fragments: set[str] = set()
    for job in jobs:
        if job.analysis:
            fragments.update(_json_fragments(job.analysis.raw_response))
        if job.application:
            fragments.update(_json_fragments(job.application.cv_json))
            texts = {}
            for doc in job.application.documents:
                fragments.update(_json_fragments(doc.extra))
                if doc.doc_type == "motivation_letter":
                    letter = {"subject": (doc.extra or {}).get("subject"), "body": doc.content}
                    fragments.update(_json_fragments(letter))
                    texts = {
                        "motivation_letter_subject": letter["subject"],
                        "motivation_letter_body": letter["body"],
                        "professional_summary": (job.application.cv_json or {}).get(
                            "professional_summary"
                        ),
                    }
                if doc.content:
                    with suppress(ValueError):
                        fragments.update(_json_fragments(json.loads(doc.content)))
            fragments.update(_json_fragments(texts))
    return keys, fragments


def _erase_batch_journal(
    session: Session, ids: set[int], keys: set[str], fragments: set[str]
) -> None:
    items = list(
        session.scalars(select(AnalysisBatchItem).where(AnalysisBatchItem.job_id.in_(ids)))
    )
    item_ids = {item.id for item in items}
    run_ids = {item.run_id for item in items}
    for item in items:
        fragments.update(_json_fragments(item.offer_data))
    for wave in session.scalars(
        select(AnalysisBatchWave).where(AnalysisBatchWave.run_id.in_(run_ids))
    ):
        requests = []
        removed_custom_ids = set()
        for request in wave.requests:
            remaining = [value for value in request["item_ids"] if value not in item_ids]
            if remaining:
                requests.append({**request, "item_ids": remaining})
            else:
                keys.add(request["cache_key"])
                removed_custom_ids.add(request["custom_id"])
        wave.requests = requests
        wave.consumed_ids = [
            value for value in wave.consumed_ids if value not in removed_custom_ids
        ]
    session.execute(delete(AnalysisBatchItem).where(AnalysisBatchItem.job_id.in_(ids)))
    for run_id in run_ids:
        if (
            session.scalar(
                select(AnalysisBatchItem.id).where(AnalysisBatchItem.run_id == run_id).limit(1)
            )
            is None
        ):
            session.execute(delete(AnalysisBatchWave).where(AnalysisBatchWave.run_id == run_id))
            session.execute(delete(AnalysisBatchRun).where(AnalysisBatchRun.id == run_id))


def _erase_exact_responses(session: Session, keys: set[str], fragments: set[str]) -> None:
    saved_results = [json.loads(fragment) for fragment in fragments]
    for cached in session.scalars(select(LLMCache)):
        matches = cached.cache_key in keys
        if not matches and fragments:
            with suppress(ValueError):
                response = json.loads(cached.response)
                matches = json.dumps(response, sort_keys=True, ensure_ascii=False) in fragments
                # Two-stage analyses were stored as one assembled result.
                # Grounding can change evidence excerpts and merge risks.
                if (
                    isinstance(response, dict)
                    and response
                    and cached.purpose
                    in {
                        "job_offer_analysis",
                        "job_fit_assessment",
                        "cv_adaptation",
                    }
                ):
                    stable = {
                        key: value
                        for key, value in response.items()
                        if key
                        not in {
                            "risks",
                            "offer_risks",
                            "supporting_excerpts",
                        }
                    }
                    matches = (
                        matches
                        or bool(stable)
                        and any(
                            all(saved.get(key) == value for key, value in stable.items())
                            for saved in saved_results
                            if isinstance(saved, dict)
                        )
                    )
        if matches:
            session.delete(cached)


def _erase_embeddings(session: Session, jobs: list[Job]) -> None:
    models = set(session.scalars(select(EmbeddingCache.model)))
    embedding_keys = {
        hashlib.sha256(f"{model}\0{text}".encode()).hexdigest()
        for model in models
        for job in jobs
        for text in (job.description, job.cleaned_description, f"{job.title}\n{job.description}")
        if text
    }
    session.execute(delete(EmbeddingCache).where(EmbeddingCache.cache_key.in_(embedding_keys)))


def erase_offer_rows(session: Session, jobs: list[Job]) -> None:
    ids = {job.id for job in jobs}
    external_ids = {job.external_id for job in jobs}
    keys, fragments = _cache_evidence(session, jobs)
    _erase_batch_journal(session, ids, keys, fragments)
    _erase_exact_responses(session, keys, fragments)
    _erase_embeddings(session, jobs)
    session.execute(
        delete(LLMUsage).where(
            or_(
                LLMUsage.job_id.in_(ids),
                LLMUsage.job_external_id.in_(external_ids),
            )
        )
    )
    session.execute(
        delete(DuplicateReviewEvent).where(
            or_(
                DuplicateReviewEvent.job_id.in_(ids),
                DuplicateReviewEvent.reference_id.in_(ids),
            )
        )
    )
    for candidate in session.scalars(
        select(Job).where(
            Job.possible_duplicate_of_id.in_(ids),
            ~Job.id.in_(ids),
        )
    ):
        candidate.possible_duplicate_of_id = None
        candidate.duplicate_review_status = JobDuplicateStatus.NONE
        candidate.duplicate_match_type = None
        candidate.duplicate_confidence = None
        from smartapply.database.repository.offer_state import synchronize_offer_state

        synchronize_offer_state(candidate)
    # ORM cascades remove scores, analyses, applications and generated documents.
    # Other job-owned tables use database ON DELETE CASCADE.
    for job in jobs:
        session.delete(job)
