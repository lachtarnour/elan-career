"""LinkedIn Jobs scraper via the Apify ``valig/linkedin-jobs-scraper`` actor."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import requests

from smartapply.config import get_source_settings
from smartapply.logging_setup import get_logger
from smartapply.offers import RawJob, make_external_id
from smartapply.parsing.cleaner import normalize_whitespace, strip_html
from smartapply.scrapers.base import Scraper, ScraperConfigError, ScraperError
from smartapply.scrapers.serpapi import normalize_date_posted
from smartapply.utils.contracts import normalize_source_contract_type

logger = get_logger(__name__)


def _should_stop(stop_requested: Callable[[], bool] | None) -> bool:
    return bool(stop_requested and stop_requested())


APIFY_LINKEDIN_JOBS_URL = (
    "https://api.apify.com/v2/actors/valig~linkedin-jobs-scraper/run-sync-get-dataset-items"
)

_DATE_POSTED_TO_APIFY = {
    "any": None,
    "today": "r86400",
    # The actor schema only exposes 24h, week and month. Keep the shared
    # Keep the app's "3days" selector valid by widening it to the closest supported
    # LinkedIn filter instead of sending an out-of-schema value.
    "3days": "r604800",
    "week": "r604800",
    "month": "r2592000",
}
_DATE_POSTED_ALLOWED_RAW = {"", "r86400", "r604800", "r2592000"}
_URL_RE = re.compile(r"https?://[^\s\])>]+", flags=re.IGNORECASE)

_CONTRACT_TYPE_ALIASES = {
    "f": "F",
    "fulltime": "F",
    "full time": "F",
    "full-time": "F",
    "temps plein": "F",
    "p": "P",
    "parttime": "P",
    "part time": "P",
    "part-time": "P",
    "temps partiel": "P",
    "c": "C",
    "contract": "C",
    "contractor": "C",
    "t": "T",
    "temporary": "T",
    "temporaire": "T",
    "i": "I",
    "internship": "I",
    "stage": "I",
    "o": "O",
    "other": "O",
    "autre": "O",
}
_EXPERIENCE_LEVEL_ALIASES = {
    "1": "1",
    "internship": "1",
    "stage": "1",
    "2": "2",
    "entry": "2",
    "entry level": "2",
    "entry-level": "2",
    "junior": "2",
    "3": "3",
    "associate": "3",
    "4": "4",
    "mid senior": "4",
    "mid-senior": "4",
    "mid senior level": "4",
    "mid-senior level": "4",
    "senior": "4",
    "5": "5",
    "director": "5",
    "directeur": "5",
    "6": "6",
    "executive": "6",
    "cadre dirigeant": "6",
}
_REMOTE_ALIASES = {
    "1": "1",
    "onsite": "1",
    "on site": "1",
    "on-site": "1",
    "sur site": "1",
    "2": "2",
    "remote": "2",
    "teletravail": "2",
    "télétravail": "2",
    "3": "3",
    "hybrid": "3",
    "hybride": "3",
}


class LinkedInJobsScraper(Scraper):
    """Search LinkedIn jobs through Apify and map results to ``RawJob``."""

    name = "linkedin"

    def __init__(
        self,
        token: str | None = None,
        *,
        url: str = APIFY_LINKEDIN_JOBS_URL,
        timeout: int | None = None,
    ):
        settings = get_source_settings()
        self.token = token or settings.apify_token
        self.url = url
        self.timeout = timeout if timeout is not None else settings.linkedin_timeout
        self.default_contract_type = _csv_list(settings.linkedin_contract_type)
        self.default_experience_level = _csv_list(settings.linkedin_experience_level)
        self.default_remote = _csv_list(settings.linkedin_remote)
        self.default_date_posted = settings.linkedin_date_posted
        self.default_max_results = settings.linkedin_max_results
        self.last_payloads: list[dict[str, Any]] = []

    def is_available(self) -> bool:
        return bool(self.token)

    def _fetch(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        # Starting an Actor is not idempotent. A lost response can leave the
        # original paid run active, so never submit the POST again automatically.
        try:
            response = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise ScraperError(
                "LinkedIn : délai de réponse Apify dépassé. La recherche peut encore "
                "être en cours sur Apify ; vérifiez son état avant de la relancer. "
                "Aucune relance automatique n’a été effectuée."
            ) from None
        except requests.RequestException:
            raise ScraperError(
                "LinkedIn : connexion à Apify interrompue. L’état de la recherche "
                "est inconnu ; vérifiez Apify avant de la relancer. "
                "Aucune relance automatique n’a été effectuée."
            ) from None
        if _is_apify_configuration_error(response):
            raise ScraperConfigError(_apify_http_error_message(response, token=self.token))
        if response.status_code >= 400:
            raise ScraperError(_apify_http_error_message(response, token=self.token))
        try:
            data = response.json()
        except ValueError:
            raise ScraperError("LinkedIn : réponse Apify invalide (JSON illisible).") from None
        if not isinstance(data, list):
            raise ScraperError("LinkedIn : réponse Apify invalide (liste d’offres attendue).")
        return [item for item in data if isinstance(item, dict)]

    def search(
        self,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = None,
        date_posted: str | None = None,
        contract_type: list[str] | tuple[str, ...] | str | None = None,
        experience_level: list[str] | tuple[str, ...] | str | None = None,
        remote: list[str] | tuple[str, ...] | str | None = None,
        url_path: str = "/jobs/search",
        stop_requested: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Iterator[RawJob]:
        if _should_stop(stop_requested):
            return
        if not self.token:
            raise ScraperConfigError("APIFY_TOKEN is not set")
        limit = self._resolve_max_results(max_results)
        if limit <= 0:
            return

        contract_values = _normalise_api_list(
            contract_type,
            default=self.default_contract_type,
            aliases=_CONTRACT_TYPE_ALIASES,
            field_name="contractType",
        )
        experience_values = _normalise_api_list(
            experience_level,
            default=self.default_experience_level,
            aliases=_EXPERIENCE_LEVEL_ALIASES,
            field_name="experienceLevel",
        )
        primary_experience, fallback_experience = _split_experience_levels(experience_values)
        remote_values = _normalise_api_list(
            remote,
            default=self.default_remote,
            aliases=_REMOTE_ALIASES,
            field_name="remote",
        )
        initial_skip_job_ids = [
            str(item).strip()
            for item in (kwargs.get("skip_job_id") or kwargs.get("skipJobId") or [])
            if str(item).strip()
        ]
        skip_job_ids = list(dict.fromkeys(initial_skip_job_ids))
        base_payload: dict[str, Any] = {
            "keywords": query.strip(),
            "location": (location or "").strip() or None,
            "datePosted": _to_apify_date_posted(
                date_posted if date_posted is not None else self.default_date_posted
            ),
            "urlPath": url_path,
        }

        self.last_payloads = []
        yielded = 0
        seen_external_ids: set[str] = set()
        experience_passes = [primary_experience]
        if fallback_experience:
            experience_passes.append(fallback_experience)

        for pass_index, experience_pass in enumerate(experience_passes, start=1):
            if _should_stop(stop_requested):
                return
            remaining = limit - yielded
            if remaining <= 0:
                return

            payload = _clean_payload(
                {
                    **base_payload,
                    "urlParam": _search_url_params(
                        contract_values, experience_pass, remote_values
                    ),
                    "limit": remaining,
                    "skipJobId": list(skip_job_ids),
                }
            )
            self.last_payloads.append(dict(payload))
            request_started = time.monotonic()
            logger.info(
                "LinkedIn Apify request start: q=%r experience=%s limit=%s timeout=%ss",
                payload.get("keywords"),
                experience_pass,
                payload.get("limit"),
                self.timeout,
            )

            try:
                items = self._fetch(payload)
            except Exception as e:
                elapsed = time.monotonic() - request_started
                detail = (
                    _request_error_summary(e)
                    if isinstance(e, requests.RequestException)
                    else _redact_secrets(_text(e))
                )
                logger.error(
                    "LinkedIn Apify request failed: q=%r experience=%s elapsed=%.1fs error=%s",
                    payload.get("keywords"),
                    experience_pass,
                    elapsed,
                    detail,
                    exc_info=True,
                )
                raise

            logger.info(
                "LinkedIn Apify request done: q=%r experience=%s items=%d elapsed=%.1fs",
                payload.get("keywords"),
                experience_pass,
                len(items),
                time.monotonic() - request_started,
            )

            skipped_experience_mismatch = 0
            for raw in items:
                if _should_stop(stop_requested):
                    return
                raw_id = _text(raw.get("id"))
                raw_experience_code = _linkedin_experience_code(raw.get("experienceLevel"))
                if (
                    experience_pass
                    and raw_experience_code
                    and raw_experience_code not in experience_pass
                ):
                    skipped_experience_mismatch += 1
                    continue
                if raw_id and raw_id not in skip_job_ids:
                    skip_job_ids.append(raw_id)
                job = self._to_raw_job(raw)
                if job is None or job.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(job.external_id)
                source_data = dict(job.source_data or {})
                source_data["_smartapply_search"] = {
                    "api_payload": payload,
                    "experience_pass": pass_index,
                    "experience_fallback_used": pass_index > 1,
                    "title": payload.get("keywords"),
                    "location": payload.get("location"),
                    "datePosted": payload.get("datePosted"),
                    "contractType": list(contract_values),
                    "experienceLevel": list(experience_pass),
                    "remote": list(remote_values),
                    "limit": payload.get("limit"),
                }
                yield job.model_copy(update={"source_data": source_data})
                yielded += 1

            # The limit belongs to the API request. Drain the complete response
            # even if Apify returns more items than requested; never discard
            # already-fetched offers to enforce a local quota.

            if skipped_experience_mismatch:
                logger.info(
                    "LinkedIn skipped %d item(s) outside experience pass %s",
                    skipped_experience_mismatch,
                    experience_pass,
                )
            if not fallback_experience or yielded >= limit:
                return

    def _resolve_max_results(self, max_results: int | None) -> int:
        limit = self.default_max_results if max_results is None else max_results
        if limit > self.default_max_results:
            raise ScraperConfigError(
                f"LinkedIn max_results exceeds LINKEDIN_MAX_RESULTS ({self.default_max_results})."
            )
        return limit

    def _to_raw_job(self, raw: dict[str, Any]) -> RawJob | None:
        title = _text(raw.get("title"))
        company = _text(raw.get("companyName")) or "Entreprise non communiquée"
        if not title:
            return None

        linkedin_url = _url_text(raw.get("url"))
        apply_url = _url_text(raw.get("applyUrl"))
        application_url = apply_url or linkedin_url or None
        external_id = _external_id(self.name, raw, title=title, company=company)

        description = _description_text(raw)
        if not description:
            return None

        contract_type = normalize_source_contract_type(raw.get("contractType"))
        remote_policy = _normalize_remote_policy(raw.get("remote"), raw.get("workType"))
        published = _parse_datetime(raw.get("postedDate"))
        apply_options = _apply_options(raw)
        source_data = dict(raw)
        source_data["_smartapply_normalized"] = {
            "url": linkedin_url,
            "applyUrl": apply_url,
            "companyUrl": _url_text(raw.get("companyUrl")),
            "recruiterUrl": _url_text(raw.get("recruiterUrl")),
            "description_source": _description_source(raw),
        }

        return RawJob(
            external_id=external_id,
            title=title,
            company=company,
            location=_text(raw.get("location")) or None,
            contract_type=contract_type,
            remote_policy=remote_policy,
            description=description,
            application_url=application_url,
            apply_options=apply_options or None,
            published_date=published,
            source=self.name,
            source_data=source_data,
        )


def _to_apify_date_posted(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if raw in _DATE_POSTED_ALLOWED_RAW:
        return raw
    normalized = normalize_date_posted(value)
    return _DATE_POSTED_TO_APIFY.get(normalized, value)


def _normalise_list(
    value: list[str] | tuple[str, ...] | str | None,
    *,
    default: list[str],
) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return _csv_list(value)
    return [str(item).strip() for item in value if str(item).strip()]


def _normalise_api_list(
    value: list[str] | tuple[str, ...] | str | None,
    *,
    default: list[str],
    aliases: dict[str, str],
    field_name: str,
) -> list[str]:
    values = _normalise_list(value, default=default)
    normalized: list[str] = []
    for item in values:
        key = _selector_key(item)
        mapped = aliases.get(key)
        if not mapped:
            allowed = ", ".join(sorted(set(aliases.values())))
            raise ScraperConfigError(
                f"Invalid LinkedIn {field_name} selector {item!r}. Allowed API codes: {allowed}."
            )
        if mapped not in normalized:
            normalized.append(mapped)
    return normalized


def _selector_key(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip().lower()
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _linkedin_experience_code(value: Any) -> str | None:
    return _EXPERIENCE_LEVEL_ALIASES.get(_selector_key(value))


def _split_experience_levels(values: list[str]) -> tuple[list[str], list[str]]:
    if "4" not in values or len(values) <= 1:
        return values, []
    primary = [value for value in values if value != "4"]
    fallback = ["4"]
    return primary, fallback


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in ("", None, [])}


def _search_url_params(
    contract_type: list[str], experience_level: list[str], remote: list[str]
) -> list[dict[str, str]]:
    """Send legacy LinkedIn selectors through the Actor's supported URL input.

    LinkedIn may ignore URL filters; the experience check on returned facts
    remains necessary and never treats a requested filter as a job fact.
    """
    return [
        {"key": key, "value": ",".join(values)}
        for key, values in (
            ("f_JT", contract_type),
            ("f_E", experience_level),
            ("f_WT", remote),
        )
        if values
    ]


def _is_apify_configuration_error(response: requests.Response) -> bool:
    status_code = response.status_code
    if status_code < 400:
        return False
    return status_code not in {408, 429} and status_code < 500


def _apify_http_error_message(response: requests.Response, *, token: str = "") -> str:
    status_code = response.status_code
    detail = _apify_error_detail(response)
    if token:
        detail = detail.replace(token, "[redacted]")
    if status_code in {401, 403}:
        explanation = "Vérifiez le jeton APIFY_TOKEN et l’accès à l’acteur LinkedIn."
    elif status_code == 402:
        explanation = "Vérifiez les crédits et l’abonnement du compte Apify."
    elif status_code == 429:
        explanation = "Quota ou limite de requêtes Apify atteint."
    elif status_code == 408 or status_code >= 500:
        explanation = (
            "La réponse de la recherche n’a pas pu être récupérée. "
            "Vérifiez l’état de l’exécution sur Apify avant de la relancer."
        )
    else:
        explanation = "Vérifiez les paramètres de recherche et l’accès à l’acteur Apify."
    message = (
        f"LinkedIn : erreur Apify HTTP {status_code}. {explanation} "
        "Aucune relance automatique n’a été effectuée."
    )
    if detail:
        message = f"{message} Détail Apify : {detail}"
    return message


def _apify_error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return _redact_secrets(_text(response.text))[:500]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            detail = _text(error.get("message")) or _text(error.get("type"))
            return _redact_secrets(detail)[:500]
        if error:
            return _redact_secrets(_text(error))[:500]
    return ""


def _request_error_summary(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, requests.Response):
        return _apify_http_error_message(response)
    return _redact_secrets(_text(exc))


def _redact_secrets(value: str) -> str:
    value = re.sub(r"(token=)[^&\s]+", r"\1[redacted]", value, flags=re.IGNORECASE)
    value = re.sub(r"apify_api_[A-Za-z0-9_-]+", "apify_api_[redacted]", value)
    value = re.sub(
        r"(Authorization:\s*Bearer\s+)[^\s,;]+",
        r"\1[redacted]",
        value,
        flags=re.IGNORECASE,
    )
    return value


def _csv_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _url_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    match = _URL_RE.search(text)
    return match.group(0) if match else ""


def _description_text(raw: dict[str, Any]) -> str:
    html_text = _html_description_text(raw)
    if html_text:
        return html_text
    return normalize_whitespace(_text(raw.get("description")))


def _html_description_text(raw: dict[str, Any]) -> str:
    html_description = _text(raw.get("descriptionHtml"))
    if html_description:
        return normalize_whitespace(strip_html(html_description))
    return ""


def _description_source(raw: dict[str, Any]) -> str:
    return "descriptionHtml" if _html_description_text(raw) else "description"


def _external_id(source: str, raw: dict[str, Any], *, title: str, company: str) -> str:
    raw_id = _text(raw.get("id"))
    if raw_id:
        return make_external_id(source, raw_id)
    linkedin_url = _url_text(raw.get("url"))
    if linkedin_url:
        return make_external_id(source, linkedin_url)
    return make_external_id(source, title, company, _text(raw.get("location")))


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_remote_policy(*values: Any) -> str | None:
    text = " ".join(_text(value).lower() for value in values)
    if "remote" in text or "télétravail" in text or "teletravail" in text:
        return "remote"
    if "hybrid" in text or "hybride" in text:
        return "hybrid"
    if "on-site" in text or "onsite" in text or "site" in text:
        return "onsite"
    return None


def _apply_options(raw: dict[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    apply_type = _text(raw.get("applyType"))
    apply_url = _url_text(raw.get("applyUrl"))
    if apply_url:
        options.append({"title": "Apply URL", "link": apply_url})
    linkedin_url = _url_text(raw.get("url"))
    if linkedin_url and linkedin_url != apply_url:
        title = "LinkedIn Easy Apply" if apply_type.upper() == "EASY_APPLY" else "LinkedIn"
        options.append({"title": title, "link": linkedin_url})
    return options


__all__ = ["APIFY_LINKEDIN_JOBS_URL", "LinkedInJobsScraper"]
