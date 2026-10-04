"""Round-robin source collection with known-offer skipping."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from requests import RequestException

from smartapply.logging_setup import get_logger
from smartapply.offers import RawJob
from smartapply.pipeline.ingest.dedupe import _KnownJobIndex
from smartapply.pipeline.ingest.reports import _CollectResult
from smartapply.scrapers.base import ScraperConfigError, ScraperError, ScraperStopError

logger = get_logger(__name__)

_RAW_SEEN_MULTIPLIER = 10
_RAW_SEEN_MIN = 50
_STRICT_API_BUDGET_SOURCES = {"serpapi"}
_PER_QUERY_SOURCES = {"linkedin": "LinkedIn", "francetravail": "France Travail"}


def collect_round_robin(
    *,
    scraper,
    source: str | None = None,
    queries: list[str],
    location: str | None,
    max_results: int | None,
    search_kwargs: dict,
    known_external_ids: set[str] | None = None,
    known_index: _KnownJobIndex | None = None,
    stop_requested: Callable[[], bool] | None = None,
    primary_query_count: int | None = None,
) -> _CollectResult:
    """Collect primary searches, then aliases fairly within each tier.

    LinkedIn and France Travail consume every planned query with ``max_results`` as the
    per-query API limit, without a global collection or raw-scan cap.
    For other sources, ``max_results`` is the global target of **new** offers (not yet in
    the DB), not a per-query quota and not a raw-fetch quota. Explicit user
    searches form the primary tier. Alias iterators are only
    opened if that tier cannot supply ``max_results`` new offers. Inside each
    tier, the round-robin keeps paginating past already-known offers until it
    reaches the target, exhausts the tier, or hits the raw-scan safety cap.

    ``known_external_ids`` is the set of offers already persisted for
    this source. Offers in that set are skipped before counting
    against ``max_results`` so a few hundred known duplicates at the
    top of the API feed do not collapse the budget to zero.
    """
    if not queries:
        return _CollectResult([], 0, 0, False)
    if _should_stop(stop_requested):
        logger.warning("Collection cancelled before iterator setup: source=%s", source)
        return _CollectResult([], 0, 0, False, cancelled=True)
    source_key = (source or getattr(scraper, "name", "")).strip().lower()
    if source_key == "linkedin":
        configured_workers = getattr(scraper, "max_concurrent_runs", 1)
        try:
            max_workers = max(1, min(int(configured_workers), 3))
        except (TypeError, ValueError):
            max_workers = 1
        return _collect_linkedin_query_batches(
            scraper=scraper,
            source_label=_PER_QUERY_SOURCES[source_key],
            queries=queries,
            location=location,
            max_results=max_results,
            search_kwargs=search_kwargs,
            known_external_ids=known_external_ids,
            known_index=known_index,
            stop_requested=stop_requested,
            max_workers=max_workers,
        )
    if source_key in _PER_QUERY_SOURCES:
        return _collect_query_batches(
            scraper=scraper,
            source_label=_PER_QUERY_SOURCES[source_key],
            queries=queries,
            location=location,
            max_results=max_results,
            search_kwargs=search_kwargs,
            known_external_ids=known_external_ids,
            known_index=known_index,
            stop_requested=stop_requested,
        )
    known = known_external_ids or set()
    known_jobs = known_index or _KnownJobIndex(frozenset(), frozenset())
    raw_jobs: list[RawJob] = []
    seen_external_ids: set[str] = set()
    skipped_known = 0
    skipped_existing = 0
    raw_seen = 0
    # Give each iterator generous room so the scraper paginates deep
    # enough to surface new offers. The round-robin enforces the real
    # cap below; the per-iterator value is just a safety bound to
    # avoid SerpApi-style overshoots.
    scraper_budget, raw_seen_cap = _collection_budget(
        source or getattr(scraper, "name", ""),
        max_results,
    )
    scraper_search_kwargs = dict(search_kwargs)
    if stop_requested is not None:
        scraper_search_kwargs["stop_requested"] = stop_requested
    hit_cap = False
    query_tiers = _query_tiers(queries, primary_query_count)
    for tier_index, tier_queries in enumerate(query_tiers):
        if tier_index:
            logger.info(
                "Primary searches exhausted below target; starting %d alias fallback(s): source=%s",
                len(tier_queries),
                source,
            )
        iterators = [
            iter(
                scraper.search(
                    concrete_query,
                    location=location,
                    max_results=scraper_budget,
                    **scraper_search_kwargs,
                )
            )
            for concrete_query in tier_queries
        ]
        active = [True] * len(iterators)
        while any(active):
            for i, iterator in enumerate(iterators):
                if not active[i]:
                    continue
                if _should_stop(stop_requested):
                    logger.warning(
                        "Collection cancelled before next item: source=%s raw_jobs=%s raw_seen=%s",
                        source,
                        len(raw_jobs),
                        raw_seen,
                    )
                    return _CollectResult(
                        raw_jobs,
                        skipped_known,
                        skipped_existing,
                        hit_cap,
                        cancelled=True,
                    )
                if max_results is not None and len(raw_jobs) >= max_results:
                    return _CollectResult(
                        raw_jobs,
                        skipped_known,
                        skipped_existing,
                        hit_cap,
                    )
                if raw_seen_cap is not None and raw_seen >= raw_seen_cap:
                    hit_cap = True
                    return _CollectResult(
                        raw_jobs,
                        skipped_known,
                        skipped_existing,
                        hit_cap,
                    )
                try:
                    raw = next(iterator)
                except StopIteration:
                    active[i] = False
                    if _should_stop(stop_requested):
                        logger.warning(
                            "Collection cancelled after iterator stopped: source=%s raw_jobs=%s raw_seen=%s",
                            source,
                            len(raw_jobs),
                            raw_seen,
                        )
                        return _CollectResult(
                            raw_jobs,
                            skipped_known,
                            skipped_existing,
                            hit_cap,
                            cancelled=True,
                        )
                    continue
                raw_seen += 1
                if raw.external_id in seen_external_ids:
                    # Intra-call deduplication (e.g. the same offer surfaced
                    # by two expanded queries) — silent, do not count.
                    continue
                seen_external_ids.add(raw.external_id)
                if raw.external_id in known:
                    # Already persisted by a previous run: skip *without*
                    # touching ``max_results`` so we keep looking for new
                    # offers further down the API feed.
                    skipped_known += 1
                    continue
                # A known source identity is safely ignorable. A known URL is
                # not dropped here: it may be a cross-source alias that must
                # be persisted and linked to the canonical offer.
                if known_jobs.matches_source_identity(raw):
                    skipped_existing += 1
                    continue
                raw_jobs.append(raw)
                if max_results is not None and len(raw_jobs) >= max_results:
                    return _CollectResult(
                        raw_jobs,
                        skipped_known,
                        skipped_existing,
                        hit_cap,
                    )
                if _should_stop(stop_requested):
                    logger.warning(
                        "Collection cancelled after accepted item: source=%s raw_jobs=%s raw_seen=%s",
                        source,
                        len(raw_jobs),
                        raw_seen,
                    )
                    return _CollectResult(
                        raw_jobs,
                        skipped_known,
                        skipped_existing,
                        hit_cap,
                        cancelled=True,
                    )
    return _CollectResult(
        raw_jobs,
        skipped_known,
        skipped_existing,
        hit_cap,
        cancelled=_should_stop(stop_requested),
    )


def _should_stop(stop_requested: Callable[[], bool] | None) -> bool:
    return bool(stop_requested and stop_requested())


@dataclass(frozen=True)
class _QueryBatchOutcome:
    index: int
    query: str
    raw_jobs: list[RawJob]
    error: Exception | None = None
    completed_run_ids: tuple[str, ...] = ()
    recovery_only: bool = False


def _collect_linkedin_query_batches(
    *,
    scraper,
    source_label: str,
    queries: list[str],
    location: str | None,
    max_results: int | None,
    search_kwargs: dict,
    known_external_ids: set[str] | None,
    known_index: _KnownJobIndex | None,
    stop_requested: Callable[[], bool] | None,
    max_workers: int,
) -> _CollectResult:
    """Drain LinkedIn queries through fixed-size parallel cohorts.

    Every cohort finishes before the next one is submitted. A fatal source
    error therefore stops all later cohorts without interrupting peers that
    were already launched. Results, duplicate checks and counters are reduced
    later on the caller thread in query order, so parallel completion cannot
    change which duplicate wins.
    """

    worker_count = min(max_workers, len(queries))
    if callable(getattr(scraper, "create_search_session", None)) and callable(
        getattr(scraper, "run_search_pass", None)
    ):
        outcomes = _run_linkedin_actor_pass_cohorts(
            scraper=scraper,
            queries=queries,
            location=location,
            max_results=max_results,
            search_kwargs=search_kwargs,
            stop_requested=stop_requested,
            worker_count=worker_count,
        )
    else:
        outcomes = _run_linkedin_query_cohorts(
            scraper=scraper,
            queries=queries,
            location=location,
            max_results=max_results,
            search_kwargs=search_kwargs,
            stop_requested=stop_requested,
            worker_count=worker_count,
        )

    return _reduce_linkedin_outcomes(
        outcomes=outcomes,
        source_label=source_label,
        known_external_ids=known_external_ids,
        known_index=known_index,
        stop_requested=stop_requested,
    )


def _run_linkedin_actor_pass_cohorts(
    *,
    scraper,
    queries: list[str],
    location: str | None,
    max_results: int | None,
    search_kwargs: dict,
    stop_requested: Callable[[], bool] | None,
    worker_count: int,
) -> dict[int, _QueryBatchOutcome]:
    """Run at most one Actor pass per worker before each cohort barrier."""

    kwargs = dict(search_kwargs)
    kwargs.pop("stop_requested", None)
    outcomes: dict[int, _QueryBatchOutcome] = {}
    fatal_stop = False
    sessions = [
        (
            index,
            query,
            scraper.create_search_session(
                query,
                location=location,
                max_results=max_results,
                stop_requested=stop_requested,
                **kwargs,
            ),
        )
        for index, query in enumerate(queries)
    ]
    recovery_sessions = [item for item in sessions if getattr(item[2], "recovery_only", False)]
    if recovery_sessions:
        # Resume the outstanding workflow before submitting any fresh search,
        # including queries earlier in the plan whose datasets were already acked.
        sessions = recovery_sessions

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="linkedin-actor",
    ) as pool:
        for cohort_start in range(0, len(sessions), worker_count):
            if fatal_stop or _should_stop(stop_requested):
                break
            cohort = sessions[cohort_start : cohort_start + worker_count]
            active = cohort
            while active and not fatal_stop and not _should_stop(stop_requested):
                futures = [
                    pool.submit(scraper.run_search_pass, session) for _, _, session in active
                ]
                wait(futures)
                next_pass: list[tuple[int, str, object]] = []
                for (index, query, session), future in zip(active, futures, strict=True):
                    pass_result = future.result()
                    previous = outcomes.get(index, _QueryBatchOutcome(index, query, []))
                    completed_run_ids = tuple(
                        dict.fromkeys((*previous.completed_run_ids, *pass_result.completed_run_ids))
                    )
                    error = previous.error or pass_result.error
                    outcomes[index] = _QueryBatchOutcome(
                        index,
                        query,
                        [*previous.raw_jobs, *pass_result.raw_jobs],
                        error,
                        completed_run_ids,
                        getattr(session, "recovery_only", False),
                    )
                    if isinstance(pass_result.error, ScraperStopError):
                        fatal_stop = True
                    elif pass_result.error is None and not pass_result.done:
                        next_pass.append((index, query, session))
                # A fallback experience pass is a new Actor cohort. Never start
                # it until every Actor in the current cohort has finished.
                active = [] if fatal_stop else next_pass
    return outcomes


def _run_linkedin_query_cohorts(
    *,
    scraper,
    queries: list[str],
    location: str | None,
    max_results: int | None,
    search_kwargs: dict,
    stop_requested: Callable[[], bool] | None,
    worker_count: int,
) -> dict[int, _QueryBatchOutcome]:
    """Compatibility path for lightweight/custom LinkedIn scraper objects."""

    kwargs = dict(search_kwargs)
    if stop_requested is not None:
        kwargs["stop_requested"] = stop_requested
    outcomes: dict[int, _QueryBatchOutcome] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="linkedin-query",
    ) as pool:
        for cohort_start in range(0, len(queries), worker_count):
            if _should_stop(stop_requested):
                break
            cohort = list(
                enumerate(
                    queries[cohort_start : cohort_start + worker_count],
                    start=cohort_start,
                )
            )
            futures = [
                pool.submit(
                    _drain_query_batch,
                    scraper=scraper,
                    index=index,
                    query=query,
                    location=location,
                    max_results=max_results,
                    search_kwargs=kwargs,
                )
                for index, query in cohort
            ]
            wait(futures)
            cohort_outcomes = [future.result() for future in futures]
            outcomes.update((outcome.index, outcome) for outcome in cohort_outcomes)
            if any(isinstance(outcome.error, ScraperStopError) for outcome in cohort_outcomes):
                break
    return outcomes


def _reduce_linkedin_outcomes(
    *,
    outcomes: dict[int, _QueryBatchOutcome],
    source_label: str,
    known_external_ids: set[str] | None,
    known_index: _KnownJobIndex | None,
    stop_requested: Callable[[], bool] | None,
) -> _CollectResult:
    """Apply deterministic deduplication after all parallel network work."""

    known = known_external_ids or set()
    known_jobs = known_index or _KnownJobIndex(frozenset(), frozenset())
    raw_jobs: list[RawJob] = []
    seen_external_ids: set[str] = set()
    skipped_known = 0
    skipped_existing = 0
    warnings: list[str] = []
    first_stop_error: Exception | None = None
    completed_run_ids: list[str] = []
    if any(outcome.recovery_only for outcome in outcomes.values()):
        warnings.append(
            "Les runs LinkedIn en attente ont été repris avec leurs paramètres "
            "d’origine. Aucun nouvel Actor n’a été lancé pendant cette récupération ; "
            "une prochaine recherche pourra démarrer après leur enregistrement."
        )

    for index in sorted(outcomes):
        outcome = outcomes[index]
        for run_id in outcome.completed_run_ids:
            if run_id not in completed_run_ids:
                completed_run_ids.append(run_id)
        for raw in outcome.raw_jobs:
            if raw.external_id in seen_external_ids:
                continue
            seen_external_ids.add(raw.external_id)
            if raw.external_id in known:
                skipped_known += 1
            elif known_jobs.matches_source_identity(raw):
                skipped_existing += 1
            else:
                raw_jobs.append(raw)
        if outcome.error is None:
            continue
        warnings.append(
            f"Requête {source_label} {outcome.query!r} incomplète : {outcome.error} "
            "Les offres déjà reçues sont conservées."
        )
        logger.warning(
            "%s query failed: q=%r error=%s",
            source_label,
            outcome.query,
            outcome.error,
        )
        if isinstance(outcome.error, ScraperStopError) and first_stop_error is None:
            first_stop_error = outcome.error

    if first_stop_error is not None:
        if not raw_jobs and not completed_run_ids:
            raise first_stop_error
        warnings.append(
            f"Les nouvelles requêtes {source_label} ont été arrêtées ; seuls les runs "
            "déjà lancés ont été finalisés."
        )

    return _CollectResult(
        raw_jobs,
        skipped_known,
        skipped_existing,
        False,
        cancelled=_should_stop(stop_requested),
        warnings=warnings,
        completed_source_run_ids=completed_run_ids,
    )


def _drain_query_batch(
    *,
    scraper,
    index: int,
    query: str,
    location: str | None,
    max_results: int | None,
    search_kwargs: dict,
) -> _QueryBatchOutcome:
    raw_jobs: list[RawJob] = []
    try:
        raw_jobs.extend(
            scraper.search(
                query,
                location=location,
                max_results=max_results,
                **search_kwargs,
            )
        )
    except (ScraperError, RequestException) as exc:
        return _QueryBatchOutcome(index, query, raw_jobs, exc)
    return _QueryBatchOutcome(index, query, raw_jobs)


def _collect_query_batches(
    *,
    scraper,
    source_label: str,
    queries: list[str],
    location: str | None,
    max_results: int | None,
    search_kwargs: dict,
    known_external_ids: set[str] | None,
    known_index: _KnownJobIndex | None,
    stop_requested: Callable[[], bool] | None,
) -> _CollectResult:
    """Drain each query's responses before opening the next query.

    The request limit never truncates the union of results. Recoverable
    failures are reported while retaining earlier offers and continuing the
    remaining queries; configuration errors stop further requests.
    """
    known = known_external_ids or set()
    known_jobs = known_index or _KnownJobIndex(frozenset(), frozenset())
    raw_jobs: list[RawJob] = []
    seen_external_ids: set[str] = set()
    skipped_known = 0
    skipped_existing = 0
    warnings: list[str] = []
    kwargs = dict(search_kwargs)
    if stop_requested is not None:
        kwargs["stop_requested"] = stop_requested
    for query in queries:
        if _should_stop(stop_requested):
            break
        try:
            for raw in scraper.search(query, location=location, max_results=max_results, **kwargs):
                if raw.external_id not in seen_external_ids:
                    seen_external_ids.add(raw.external_id)
                    if raw.external_id in known:
                        skipped_known += 1
                    elif known_jobs.matches_source_identity(raw):
                        skipped_existing += 1
                    else:
                        raw_jobs.append(raw)
                if _should_stop(stop_requested):
                    break
        except (ScraperError, RequestException) as exc:
            warnings.append(
                f"Requête {source_label} {query!r} incomplète : {exc} "
                "Les offres déjà reçues sont conservées."
            )
            logger.warning("%s query failed: q=%r error=%s", source_label, query, exc)
            response = getattr(exc, "response", None)
            configuration_error = isinstance(exc, ScraperConfigError) or (
                response is not None and response.status_code in {400, 401, 402, 403}
            )
            if configuration_error:
                if not raw_jobs:
                    raise
                warnings.append(f"Les requêtes {source_label} restantes n’ont pas été lancées.")
                break
    return _CollectResult(
        raw_jobs,
        skipped_known,
        skipped_existing,
        False,
        cancelled=_should_stop(stop_requested),
        warnings=warnings,
    )


def _query_tiers(
    queries: list[str],
    primary_query_count: int | None,
) -> tuple[list[str], ...]:
    if primary_query_count is None or primary_query_count >= len(queries):
        return (queries,)
    boundary = max(1, primary_query_count)
    return (queries[:boundary], queries[boundary:])


def _collection_budget(
    source: str,
    max_results: int | None,
) -> tuple[int | None, int | None]:
    if max_results is None:
        return None, None
    if source.strip().lower() in _STRICT_API_BUDGET_SOURCES:
        return max_results, max_results
    budget = max(_RAW_SEEN_MIN, max_results * _RAW_SEEN_MULTIPLIER)
    return budget, budget
