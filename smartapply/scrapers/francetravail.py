"""France Travail (ex Pôle emploi) — Offres d'emploi v2 API.

Documentation: https://francetravail.io/data/api/offres-emploi

Auth: OAuth 2.0 client_credentials. The token is cached in-memory for its
lifetime (1500s default).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
from threading import Thread
from typing import Any

import requests

from smartapply.config import get_source_settings
from smartapply.logging_setup import get_logger
from smartapply.offers import RawJob, make_external_id
from smartapply.scrapers.base import Scraper, ScraperConfigError, ScraperError
from smartapply.scrapers.francetravail_experience import (
    _extract_experience,
    _format_experience_section,
)
from smartapply.utils.contracts import normalize_source_contract_type
from smartapply.utils.geo.resolver import resolve_french_location

logger = get_logger(__name__)


def _should_stop(stop_requested: Callable[[], bool] | None) -> bool:
    return bool(stop_requested and stop_requested())


class FranceTravailSearchCancelled(RuntimeError):
    """Raised internally when a cooperative stop abandons an in-flight FT request."""


TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"

# Map the shared SerpApi-style freshness tokens (``today``/``3days``/``week``/``month``)
# to the equivalent rolling window in days. France Travail's API uses
# ``minCreationDate`` (ISO 8601 UTC), not chips, so we derive the threshold here.
_DATE_POSTED_TO_DAYS = {
    "today": 1,
    "3days": 3,
    "week": 7,
    "month": 30,
}


def _date_posted_to_creation_window(
    date_posted: str | None, *, now: datetime | None = None
) -> tuple[str, str] | None:
    """Convert a ``date_posted`` token to a ``(min, max)`` ISO creation window.

    ``any`` (or unknown) returns ``None`` so no filter is appended. The FT API
    requires ``minCreationDate`` and ``maxCreationDate`` to be sent together
    (error 1780533709701), so the helper always returns the pair when active.

    ``now`` is injectable so tests can lock the window instead of relying on
    wall-clock. Format is the only one accepted by FT: ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    if not date_posted:
        return None
    # Reuse the SerpApi normaliser so both scrapers accept the same aliases
    # (``lastweek``, ``7days``, etc.) without redefining them.
    from smartapply.scrapers.serpapi import normalize_date_posted

    normalized = normalize_date_posted(date_posted)
    days = _DATE_POSTED_TO_DAYS.get(normalized)
    if days is None:
        return None
    reference = now or datetime.now(tz=timezone.utc)
    threshold = reference - timedelta(days=days)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return threshold.strftime(fmt), reference.strftime(fmt)


# ---- Location resolution -----------------------------------------------------
# Free-text locations ("Paris", "Île-de-France", "La Défense", …) are mapped
# to FT's structured filters via :mod:`smartapply.utils.geo.resolver`, which is
# backed by the open ``geo.api.gouv.fr`` referential. Without this layer the
# old code path silently appended the location to ``motsCles``, which made
# "Paris, France" return only offers whose text literally contained the word
# "Paris" — about a third of the real Paris job pool.


def _resolve_ft_location_params(
    location: str | None,
) -> tuple[dict[str, str], str | None]:
    """Translate a free-text location to FT API params.

    Returns ``(structured, keyword)``:

    - ``structured``: dict of FT params (``region`` / ``departement`` /
      ``commune``) to merge into the request. Empty when no structured
      match is available.
    - ``keyword``: the string to append to ``motsCles`` when the location
      could not be resolved. ``None`` means "no location filter at all"
      (national search).

    Resolution rules live in :func:`resolve_french_location`. This function
    is a thin adapter that maps the generic :class:`ResolvedLocation` to
    the parameter names the FT API understands.
    """
    resolved = resolve_french_location(location)
    if resolved.national:
        return ({}, None)
    if resolved.region:
        return ({"region": resolved.region}, None)
    if resolved.departement:
        return ({"departement": resolved.departement}, None)
    if resolved.commune:
        return ({"commune": resolved.commune}, None)
    # Unresolved: preserve the previous keyword-append behaviour so we
    # never silently lose recall on a typo or an unmapped place.
    return ({}, resolved.unresolved or location)


class FranceTravailScraper(Scraper):
    name = "francetravail"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        timeout: int | None = None,
    ):
        settings = get_source_settings()
        self.client_id = client_id or settings.francetravail_client_id
        self.client_secret = client_secret or settings.francetravail_client_secret
        self.scope = scope or settings.francetravail_scope
        self.timeout = timeout if timeout is not None else settings.francetravail_timeout
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def is_available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # -------------------- auth --------------------

    def _get_token(self, *, stop_requested: Callable[[], bool] | None = None) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        if not self.is_available():
            raise ScraperConfigError(
                "FRANCETRAVAIL_CLIENT_ID/SECRET must be set to use this scraper"
            )
        response = self._request_with_cancel(
            "POST",
            TOKEN_URL,
            operation="France Travail token request",
            stop_requested=stop_requested,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + payload.get("expires_in", 1500)
        return self._token

    # -------------------- search --------------------

    def _request_with_cancel(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        stop_requested: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        if _should_stop(stop_requested):
            raise FranceTravailSearchCancelled(f"{operation} cancelled before request")
        if stop_requested is None:
            request_fn = requests.post if method.upper() == "POST" else requests.get
            return request_fn(url, **kwargs)

        result_queue: Queue[tuple[str, Any]] = Queue(maxsize=1)
        request_fn = requests.post if method.upper() == "POST" else requests.get

        def _worker() -> None:
            try:
                result_queue.put(("response", request_fn(url, **kwargs)))
            except Exception as exc:  # pragma: no cover - relayed to caller
                result_queue.put(("error", exc))

        thread = Thread(target=_worker, name="francetravail-http", daemon=True)
        thread.start()
        while True:
            if _should_stop(stop_requested):
                logger.warning("%s cancelled while HTTP request is still in-flight", operation)
                raise FranceTravailSearchCancelled(operation)
            try:
                kind, value = result_queue.get(timeout=0.2)
            except Empty:
                continue
            if kind == "error":
                raise value
            return value

    def _fetch_range(
        self,
        *,
        params: dict[str, Any],
        headers: dict[str, str],
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        request_started = time.monotonic()
        logger.info(
            "France Travail request start: q=%r range=%s timeout=%ss",
            params.get("motsCles"),
            params.get("range"),
            self.timeout,
        )
        response = self._request_with_cancel(
            "GET",
            SEARCH_URL,
            operation=(
                "France Travail search request "
                f"q={params.get('motsCles')!r} range={params.get('range')}"
            ),
            stop_requested=stop_requested,
            params=params,
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code == 204:
            logger.info(
                "France Travail request done: q=%r range=%s status=204 results=0 elapsed=%.1fs",
                params.get("motsCles"),
                params.get("range"),
                time.monotonic() - request_started,
            )
            return {}
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error(
                "France Travail response invalid: q=%r range=%s status=%s elapsed=%.1fs error=%s",
                params.get("motsCles"),
                params.get("range"),
                response.status_code,
                time.monotonic() - request_started,
                exc,
                exc_info=True,
            )
            raise
        if not isinstance(payload, dict):
            exc = ScraperError("France Travail response invalid: expected a JSON object")
            logger.error(
                "France Travail response invalid: q=%r range=%s status=%s elapsed=%.1fs error=%s",
                params.get("motsCles"),
                params.get("range"),
                response.status_code,
                time.monotonic() - request_started,
                exc,
                exc_info=True,
            )
            raise exc
        if payload.get("error"):
            detail = payload.get("error")
            exc = ScraperError(f"France Travail returned an API error: {str(detail)[:500]}")
            logger.error(
                "France Travail API error: q=%r range=%s status=%s elapsed=%.1fs error=%s",
                params.get("motsCles"),
                params.get("range"),
                response.status_code,
                time.monotonic() - request_started,
                exc,
            )
            raise exc
        results = payload.get("resultats")
        if not isinstance(results, list):
            exc = ScraperError("France Travail response invalid: missing resultats list")
            logger.error(
                "France Travail response invalid: q=%r range=%s status=%s elapsed=%.1fs error=%s",
                params.get("motsCles"),
                params.get("range"),
                response.status_code,
                time.monotonic() - request_started,
                exc,
                exc_info=True,
            )
            raise exc
        logger.info(
            "France Travail request done: q=%r range=%s status=%s results=%s elapsed=%.1fs",
            params.get("motsCles"),
            params.get("range"),
            response.status_code,
            len(results),
            time.monotonic() - request_started,
        )
        return payload

    def search(
        self,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = None,
        commune: str | None = None,
        departement: str | None = None,
        type_contrat: str | None = None,
        date_posted: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Iterator[RawJob]:
        if _should_stop(stop_requested):
            logger.warning("France Travail search cancelled before start: q=%r", query)
            return
        if max_results is not None and max_results <= 0:
            return

        try:
            token = self._get_token(stop_requested=stop_requested)
        except FranceTravailSearchCancelled:
            logger.warning("France Travail search cancelled during auth: q=%r", query)
            return
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        creation_window = _date_posted_to_creation_window(date_posted)

        # Resolve the free-text location once. Explicit ``commune`` /
        # ``departement`` kwargs always win — the resolver only runs when the
        # caller did not supply a structured filter itself.
        if commune or departement:
            structured_location: dict[str, str] = {}
            keyword_location: str | None = None
        else:
            structured_location, keyword_location = _resolve_ft_location_params(location)

        # France Travail paginates by `range=start-end`, max 150 per request.
        page_size = 50
        results_yielded = 0
        offset = 0

        while True:
            if _should_stop(stop_requested):
                logger.warning(
                    "France Travail search cancelled before page fetch: q=%r offset=%s yielded=%s",
                    query,
                    offset,
                    results_yielded,
                )
                return
            request_size = min(page_size, 1150 - offset)
            if max_results is not None:
                request_size = min(request_size, max_results - results_yielded)
            end = offset + request_size - 1
            params: dict[str, Any] = {
                "motsCles": query,
                "range": f"{offset}-{end}",
            }
            if commune:
                params["commune"] = commune
            if departement:
                params["departement"] = departement
            for structured_key, structured_value in structured_location.items():
                params.setdefault(structured_key, structured_value)
            if type_contrat:
                params["typeContrat"] = type_contrat
            if creation_window:
                params["minCreationDate"], params["maxCreationDate"] = creation_window
            if keyword_location:
                params["motsCles"] = f"{query} {keyword_location}".strip()

            try:
                payload = self._fetch_range(
                    params=params,
                    headers=headers,
                    stop_requested=stop_requested,
                )
            except FranceTravailSearchCancelled:
                logger.warning(
                    "France Travail search cancelled during request: q=%r yielded=%s",
                    query,
                    results_yielded,
                )
                return
            except requests.RequestException as e:
                logger.error(
                    "France Travail request failed: q=%r range=%s error=%s",
                    params.get("motsCles"),
                    params.get("range"),
                    e,
                    exc_info=True,
                )
                if _should_stop(stop_requested):
                    logger.warning(
                        "France Travail search stopped after request failure: q=%r yielded=%s",
                        query,
                        results_yielded,
                    )
                    return
                raise
            if _should_stop(stop_requested):
                logger.warning(
                    "France Travail search cancelled after request before parsing jobs: "
                    "q=%r yielded=%s",
                    query,
                    results_yielded,
                )
                return

            jobs = payload.get("resultats") or []
            if not jobs:
                break

            for raw in jobs:
                if _should_stop(stop_requested):
                    logger.warning(
                        "France Travail search cancelled while reading jobs: q=%r yielded=%s",
                        query,
                        results_yielded,
                    )
                    return
                job = self._to_raw_job(raw)
                if job is None:
                    continue
                yield job
                results_yielded += 1

            # Bound the request, not the response: retain every returned offer
            # even if the API sends more rows than the requested range.
            if max_results is not None and results_yielded >= max_results:
                return
            offset += request_size
            # 1149 is the maximum offset supported by FT API
            if len(jobs) < request_size or offset > 1149:
                break

    # -------------------- mapping --------------------

    def _to_raw_job(self, raw: dict[str, Any]) -> RawJob | None:
        title = raw.get("intitule")
        entreprise = raw.get("entreprise") or {}
        company = entreprise.get("nom") or "Entreprise non communiquée"
        if not title:
            return None

        lieu_travail = raw.get("lieuTravail") or {}
        location = lieu_travail.get("libelle")

        # Many FT offers leave ``entreprise.nom`` empty but ship the real hiring
        # entity name (or a "Name - tagline" header) in ``entreprise.description``.
        # Prepend that text so the LLM extractor — and human reviewers — see it.
        entreprise_desc = (entreprise.get("description") or "").strip()
        company_header = f"À propos de l'entreprise :\n{entreprise_desc}" if entreprise_desc else ""
        experience = _extract_experience(raw)
        experience_section = _format_experience_section(experience)

        description_parts = [
            company_header,
            experience_section,
            raw.get("description") or "",
            raw.get("competences")
            and "Compétences:\n- "
            + "\n- ".join(
                f"{c.get('libelle', '')} ({c.get('exigence', '')})"
                for c in (raw.get("competences") or [])
            ),
            raw.get("qualitesProfessionnelles")
            and "Qualités:\n- "
            + "\n- ".join(
                q.get("libelle", "") for q in (raw.get("qualitesProfessionnelles") or [])
            ),
        ]
        description = "\n\n".join(p.strip() for p in description_parts if p)

        contract_type = normalize_source_contract_type(
            raw.get("typeContratLibelle") or raw.get("natureContrat")
        )
        remote_policy: str | None = None
        if (
            isinstance(raw.get("trancheSalaire"), str)
            and "télétravail" in raw.get("trancheSalaire", "").lower()
        ):
            remote_policy = "remote"

        published: datetime | None = None
        if pub := raw.get("dateCreation"):
            try:
                published = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                published = None

        application_url = raw.get("origineOffre", {}).get("urlOrigine")
        ext_id = raw.get("id") or ""
        # France Travail's offer id is the durable identity.  Titles and
        # company labels can be corrected by the source between fetches and
        # must not create a second job row.
        if ext_id:
            external_id = make_external_id(self.name, str(ext_id))
        elif application_url:
            external_id = make_external_id(self.name, application_url)
        else:
            external_id = make_external_id(self.name, title, company, location or "")

        source_data = dict(raw)
        if experience:
            source_data["_smartapply_experience"] = experience

        return RawJob(
            external_id=external_id,
            title=title.strip(),
            company=company.strip(),
            location=location,
            contract_type=contract_type,
            remote_policy=remote_policy,
            description=description,
            experience=experience,
            application_url=application_url,
            published_date=published,
            source=self.name,
            source_data=source_data,
        )
