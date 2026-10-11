"""LLM cache and usage repository helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from smartapply.database.models import Job, LLMCache, LLMUsage


def cache_get(session: Session, cache_key: str) -> LLMCache | None:
    return session.execute(
        select(LLMCache).where(LLMCache.cache_key == cache_key)
    ).scalar_one_or_none()


def purge_expired_cache(
    session: Session,
    *,
    ttl_days: int,
    now: datetime | None = None,
) -> int:
    """Delete exact LLM responses older than the configured retention window."""
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=ttl_days)
    result = session.execute(delete(LLMCache).where(LLMCache.created_at < cutoff))
    return int(result.rowcount or 0)


def cache_set(
    session: Session,
    *,
    cache_key: str,
    model: str,
    response: str,
    prompt_tokens: int,
    completion_tokens: int,
    purpose: str | None = None,
    provider: str = "openai",
) -> LLMCache:
    entry = cache_get(session, cache_key)
    if entry is not None:
        # Refresh stale/invalid entries (for example after a schema change).
        # This also makes a paid recovery reusable on the next request.
        entry.model = model
        entry.provider = provider
        entry.response = response
        entry.prompt_tokens = prompt_tokens
        entry.completion_tokens = completion_tokens
        entry.purpose = purpose
        # A paid refresh starts a new retention window. Cache hits do not call
        # ``cache_set``, so ordinary reads never extend an entry indefinitely.
        entry.created_at = datetime.now(timezone.utc)
        return entry
    entry = LLMCache(
        cache_key=cache_key,
        model=model,
        provider=provider,
        response=response,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        purpose=purpose,
    )
    session.add(entry)
    session.flush()
    return entry


def record_usage(
    session: Session,
    *,
    purpose: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    cached_prompt_tokens: int = 0,
    cache_write_prompt_tokens: int = 0,
    cached: bool = False,
    job_id: int | None = None,
    cache_key: str | None = None,
    provider: str = "openai",
    requested_model: str | None = None,
    reasoning_effort: str | None = None,
) -> LLMUsage:
    job_external_id = None
    if job_id is not None:
        job_external_id = session.scalar(select(Job.external_id).where(Job.id == job_id))
    usage = LLMUsage(
        purpose=purpose,
        model=model,
        requested_model=requested_model,
        reasoning_effort=reasoning_effort,
        provider=provider,
        prompt_tokens=prompt_tokens,
        cached_prompt_tokens=cached_prompt_tokens,
        cache_write_prompt_tokens=cache_write_prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        cached=cached,
        job_id=job_id,
        job_external_id=job_external_id,
        cache_key=cache_key,
    )
    session.add(usage)
    return usage


def total_cost(session: Session) -> float:
    rows: Iterable[LLMUsage] = session.execute(select(LLMUsage)).scalars()
    return float(sum(row.cost_usd for row in rows))
