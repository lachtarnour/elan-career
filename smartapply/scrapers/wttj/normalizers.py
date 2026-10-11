"""WTTJ normalization and shared parsing helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

from smartapply.scrapers.wttj.contracts import (
    JOB_PATH_RE,
    WTTJ_BASE_URL,
    WTTJ_SOURCE,
)
from smartapply.utils.contracts import normalize_source_contract_type


def _format_api_offices(value: Any) -> str | None:
    offices = value if isinstance(value, list) else [value]
    formatted: list[str] = []
    for office in offices:
        if not isinstance(office, dict):
            continue
        parts = [
            _as_text(office.get("local_city") or office.get("city")),
            _as_text(office.get("district")),
            _as_text(office.get("country_code") or office.get("country")),
        ]
        text = ", ".join(part for part in parts if part)
        if text and text not in formatted:
            formatted.append(text)
    return "; ".join(formatted) or None


def _normalize_api_contract_type(value: Any) -> str | None:
    text = _as_text(value)
    if not text:
        return None
    return normalize_source_contract_type(text, source=WTTJ_SOURCE) or text


def _normalize_api_remote_policy(value: Any) -> str | None:
    text = _clean_text(str(value or "").replace("_", " ").lower())
    if not text or text == "unknown":
        return None
    if text in {"fulltime", "full time", "full remote", "remote"}:
        return "remote"
    if text in {"partial", "partiel", "hybrid", "hybride"}:
        return "hybrid"
    if text in {"no", "none", "onsite", "not allowed"}:
        return "onsite"
    return text


def _experience_min_years_from_api_level(value: str | None) -> float | None:
    normalized = (value or "").strip().upper()
    mapping = {
        "NO_EXPERIENCE": 0.0,
        "LESS_THAN_1_YEAR": 0.0,
        "1_TO_2_YEARS": 1.0,
        "3_TO_4_YEARS": 3.0,
        "5_TO_10_YEARS": 5.0,
        "MORE_THAN_10_YEARS": 10.0,
    }
    return mapping.get(normalized)


def _api_company_summary(payload: dict[str, Any]) -> str | None:
    return (
        _clean_text(_description_to_text(_as_text(payload.get("company_summary"))))
        or _clean_text(_description_to_text(_as_text(payload.get("company_description"))))
        or _clean_text(
            _description_to_text(_as_text(_dict(payload.get("organization")).get("description")))
        )
    )


def _format_api_sectors(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    names = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _localized_text(item.get("name"))
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or None


def _localized_text(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("fr", "en"):
            text = _as_text(value.get(key))
            if text:
                return text
        for raw in value.values():
            text = _as_text(raw)
            if text:
                return text
        return None
    return _as_text(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _cookie_value(cookie_header: str, name: str) -> str | None:
    for part in cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value
    return None


def _job_url_from_api_item(job: dict[str, Any]) -> str | None:
    for key in ("url", "absolute_url", "web_url"):
        url = _canonical_job_url(_as_text(job.get(key)))
        if url:
            return url

    organization = job.get("organization")
    if not isinstance(organization, dict):
        return None
    organization_slug = _as_text(organization.get("slug"))
    job_slug = _as_text(job.get("slug"))
    if not organization_slug or not job_slug:
        return None
    return _canonical_job_url(f"{WTTJ_BASE_URL}/fr/companies/{organization_slug}/jobs/{job_slug}")


def _first_json_ld(soup: BeautifulSoup, json_type: str) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in _json_ld_items(payload):
            if item.get("@type") == json_type:
                return item
    return None


def _json_ld_items(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("@graph"), list):
            yield from (item for item in payload["@graph"] if isinstance(item, dict))
        else:
            yield payload
    elif isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict))


def _faq_answers(soup: BeautifulSoup) -> dict[str, str]:
    faq = _first_json_ld(soup, "FAQPage")
    if not faq:
        return {}
    answers: dict[str, str] = {}
    for entity in faq.get("mainEntity") or []:
        if not isinstance(entity, dict):
            continue
        question = _as_text(entity.get("name"))
        accepted = entity.get("acceptedAnswer")
        answer = _as_text(accepted.get("text")) if isinstance(accepted, dict) else None
        if question and answer:
            answers[question] = answer
    return answers


def _video_titles(soup: BeautifulSoup) -> list[str]:
    titles: list[str] = []
    for block in soup.select(
        '[data-testid="block-videos-item"], [data-testid="organization-content-block-video"]'
    ):
        text = _clean_text(block.get_text(" ", strip=True))
        if text and text not in titles:
            titles.append(text)
    return titles


def _normalize_remote_policy(remote_text: str | None) -> str | None:
    if not remote_text:
        return None
    value = remote_text.lower()
    if "total" in value:
        return "remote"
    if "fréquent" in value or "frequent" in value or "occasionnel" in value or "autorisé" in value:
        return "hybrid"
    if "pas" in value or "non" in value:
        return "onsite"
    return remote_text


def _description_to_text(description_html: str | None) -> str:
    if not description_html:
        return ""
    soup = BeautifulSoup(description_html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return _clean_text(soup.get_text("\n", strip=True))


def _format_locations(job_location: Any) -> str | None:
    locations = job_location if isinstance(job_location, list) else [job_location]
    formatted: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address")
        if not isinstance(address, dict):
            continue
        locality = _as_text(address.get("addressLocality"))
        region = _as_text(address.get("addressRegion"))
        country = _as_text(address.get("addressCountry"))
        parts = [locality]
        if region and region != locality:
            parts.append(region)
        if country:
            parts.append(country)
        text = ", ".join(part for part in parts if part)
        if text:
            formatted.append(text)
    return "; ".join(formatted) or None


def _canonical_job_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc and not parsed.netloc.endswith("welcometothejungle.com"):
        return None
    if not JOB_PATH_RE.search(parsed.path):
        return None
    return urlunparse(("https", "www.welcometothejungle.com", parsed.path, "", "", ""))


def _canonical_company_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc and not parsed.netloc.endswith("welcometothejungle.com"):
        return None
    match = re.fullmatch(r"/(?:fr|en)/companies/[^/?#]+", parsed.path)
    if not match:
        return None
    return urlunparse(("https", "www.welcometothejungle.com", parsed.path, "", "", ""))


def _detail_canonical_url(soup: BeautifulSoup) -> str | None:
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and canonical.get("href"):
        url = _canonical_job_url(canonical["href"])
        if url:
            return url
    return _canonical_job_url(_meta_content(soup, "og:url"))


def _company_canonical_url(soup: BeautifulSoup) -> str | None:
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and canonical.get("href"):
        url = _canonical_company_url(canonical["href"])
        if url:
            return url
    return _canonical_company_url(_meta_content(soup, "og:url"))


def _href_for_selector(soup: BeautifulSoup, selector: str) -> str | None:
    element = soup.select_one(selector)
    if not element:
        return None
    href = element.get("href")
    return _as_text(href)


def _text_for_selector(soup: BeautifulSoup, selector: str) -> str | None:
    element = soup.select_one(selector)
    return _clean_text(element.get_text(" ", strip=True)) if element else None


def _block_after_heading(
    soup: BeautifulSoup,
    heading: str,
    *,
    parent_steps: int,
) -> Any | None:
    node = _visible_heading_node(soup, heading)
    if not node:
        return None
    block: Any = node.parent
    for _ in range(parent_steps):
        if block and block.parent:
            block = block.parent
    return block


def _visible_heading_node(soup: BeautifulSoup, heading: str) -> Any | None:
    for node in soup.find_all(string=lambda value: bool(value and heading in value)):
        parent = node.parent
        if not parent:
            continue
        if parent.find_parent(["script", "style", "noscript"]):
            continue
        text = _clean_text(str(node))
        if text == heading or heading in text:
            return node
    return None


def _leaf_texts(
    block: Any,
    *,
    tags: tuple[str, ...] = ("span", "p", "a", "h2", "h3", "h4", "div", "li"),
) -> list[str]:
    texts: list[str] = []
    for node in block.find_all(tags):
        if node.find([tag for tag in tags]):
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if text and text not in texts:
            texts.append(text)
    return texts


def _strip_heading(text: str, heading: str) -> str | None:
    if text.startswith(heading):
        text = text[len(heading) :].strip()
    return text or None


def _is_external_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and not parsed.netloc.endswith(
        "welcometothejungle.com"
    )


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url if re.match(r"^https?://", url) else f"https://{url}")
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _meta_content(soup: BeautifulSoup, property_name: str) -> str | None:
    tag = soup.select_one(f'meta[property="{property_name}"]')
    return _as_text(tag.get("content")) if tag else None


def _company_from_title_tag(soup: BeautifulSoup) -> str | None:
    if not soup.title:
        return None
    parts = [part.strip() for part in soup.title.get_text(" ", strip=True).split(" - ")]
    if len(parts) >= 2:
        return parts[-2]
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _safe_filename(value: str) -> str:
    filename = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return filename[:120] or "wttj-job"


def _wait_for_wttj_content(page: Any, timeout_ms: int, timeout_error: type[Exception]) -> None:
    with suppress(timeout_error):
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10_000))
    with suppress(timeout_error):
        page.wait_for_selector(
            'a[href*="/fr/companies/"][href*="/jobs/"], '
            'script[type="application/ld+json"], '
            '[data-testid="job-section-description"]',
            timeout=timeout_ms,
        )


def _external_company_website(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("url", "website", "external_url"):
            text = _as_text(value.get(key))
            if text and _is_external_url(text):
                return text
    text = _as_text(value)
    if text and _is_external_url(text):
        return text
    return None
