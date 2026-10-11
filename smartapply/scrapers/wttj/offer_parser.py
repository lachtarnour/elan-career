"""WTTJ single-offer parsing."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from smartapply.offers import RawJob, make_external_id
from smartapply.scrapers.wttj.company_hydration import (
    _company_profile_from_detail_api,
    _company_profile_url,
    _company_stats_from_job_page,
    _company_summary_from_job_page,
    _company_tags,
    _company_website,
    _section_items_by_heading,
    _social_links,
    _workplace,
)
from smartapply.scrapers.wttj.contracts import (
    CONTRACT_LABELS,
    WTTJ_BASE_URL,
    WTTJ_HEADERS,
    WTTJ_ORGANIZATIONS_API_URL,
    WTTJ_SOURCE,
    WTTJScraperError,
)
from smartapply.scrapers.wttj.normalizers import (
    _api_company_summary,
    _as_text,
    _block_after_heading,
    _canonical_company_url,
    _canonical_job_url,
    _clean_text,
    _company_from_title_tag,
    _description_to_text,
    _detail_canonical_url,
    _dict,
    _domain_from_url,
    _experience_min_years_from_api_level,
    _faq_answers,
    _first_json_ld,
    _format_api_offices,
    _format_locations,
    _leaf_texts,
    _localized_text,
    _meta_content,
    _normalize_api_contract_type,
    _normalize_api_remote_policy,
    _normalize_remote_policy,
    _parse_datetime,
    _video_titles,
)


def parse_detail_html(html: str, *, url: str | None = None) -> RawJob:
    """Parse one WTTJ job detail page into the project's canonical RawJob."""
    soup = BeautifulSoup(html, "lxml")
    job_posting = _first_json_ld(soup, "JobPosting")
    if not job_posting:
        raise WTTJScraperError("No schema.org JobPosting JSON-LD found in WTTJ detail page")

    faq = _faq_answers(soup)
    metadata = _metadata_text(soup)
    organization = job_posting.get("hiringOrganization") or {}
    company_profile_url = _company_profile_url(soup)

    title = _as_text(job_posting.get("title")) or _meta_content(soup, "og:title") or "Untitled job"
    company = (
        _as_text(organization.get("name")) or _company_from_title_tag(soup) or "Unknown company"
    )
    description = _description_to_text(_as_text(job_posting.get("description")))
    location = _format_locations(job_posting.get("jobLocation"))
    contract_type = _contract_type(metadata, faq, job_posting)
    remote_text = _remote_text(metadata, faq)
    remote_policy = _normalize_remote_policy(remote_text)
    application_url = _canonical_job_url(url) if url else _detail_canonical_url(soup)
    published_date = _parse_datetime(_as_text(job_posting.get("datePosted")))
    company_website = _company_website(soup, organization)

    if not description:
        description = _clean_text(soup.get_text("\n", strip=True))

    source_data: dict[str, Any] = {
        "url": application_url,
        "industry": job_posting.get("industry"),
        "valid_through": job_posting.get("validThrough"),
        "employment_type": job_posting.get("employmentType"),
        "remote_text": remote_text,
        "metadata_text": metadata,
        "hiring_organization": organization,
        "company_profile_url": company_profile_url,
        "company_website": company_website,
        "company_domain": _domain_from_url(company_website),
        "company_social_links": _social_links(soup),
        "company_summary": _company_summary_from_job_page(soup),
        "company_tags": _company_tags(soup),
        "company_stats": _company_stats_from_job_page(soup),
        "skills": _skills(soup),
        "skills_more_count": _skills_more_count(soup),
        "workplace": _workplace(soup),
        "perks_and_benefits": _section_items_by_heading(soup, "Les avantages salariés"),
        "certifications": _section_items_by_heading(soup, "Engagements"),
        "videos": _video_titles(soup),
        "json_ld": job_posting,
    }
    if faq:
        source_data["faq"] = faq

    external_key = application_url or f"{company}|{title}|{location or ''}"
    return RawJob(
        external_id=make_external_id(WTTJ_SOURCE, external_key),
        title=title,
        company=company,
        location=location,
        contract_type=contract_type,
        remote_policy=remote_policy,
        description=description,
        application_url=application_url,
        published_date=published_date,
        source=WTTJ_SOURCE,
        source_data=source_data,
    )


def fetch_detail_api_job(
    url: str,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    """Fetch one public WTTJ job detail from the JSON API."""
    api_url = _detail_api_url_from_job_url(url)
    if not api_url:
        raise WTTJScraperError(f"Cannot build WTTJ detail API URL for {url}")

    headers = dict(WTTJ_HEADERS)
    headers["Accept"] = "application/json, text/plain, */*"
    response = requests.get(api_url, headers=headers, timeout=timeout)
    response.raise_for_status()
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise WTTJScraperError("WTTJ detail API did not return JSON.") from exc
    job = payload.get("job") if isinstance(payload, dict) else None
    if not isinstance(job, dict):
        raise WTTJScraperError("Unexpected WTTJ detail API payload.")
    return job


def parse_detail_api_job(payload: dict[str, Any], *, url: str | None = None) -> RawJob:
    """Parse one WTTJ detail API job payload into ``RawJob``."""
    organization = _dict(payload.get("organization"))
    company_slug = _as_text(organization.get("slug"))
    application_url = _canonical_job_url(url) or (
        _canonical_job_url(
            f"{WTTJ_BASE_URL}/fr/companies/{company_slug}/jobs/{payload.get('slug')}"
        )
        if company_slug and payload.get("slug")
        else None
    )
    title = _as_text(payload.get("name")) or "Untitled job"
    company = _as_text(organization.get("name")) or "Unknown company"
    description = _api_job_description(payload)
    location = _format_api_offices(payload.get("offices"))
    contract_type = _normalize_api_contract_type(payload.get("contract_type"))
    remote_policy = _normalize_api_remote_policy(payload.get("remote"))
    published_date = _parse_datetime(_as_text(payload.get("published_at")))
    company_profile_url = (
        _canonical_company_url(f"{WTTJ_BASE_URL}/fr/companies/{company_slug}")
        if company_slug
        else None
    )
    company_profile = _company_profile_from_detail_api(payload, company_profile_url)
    company_website = _as_text(company_profile.get("website"))

    source_data: dict[str, Any] = {
        "url": application_url,
        "detail_api": payload,
        "industry": _localized_text(_dict(payload.get("profession")).get("category_name")),
        "valid_through": None,
        "employment_type": payload.get("contract_type"),
        "remote_text": payload.get("remote"),
        "metadata_text": "",
        "hiring_organization": organization,
        "company_profile_url": company_profile_url,
        "company_website": company_website,
        "company_domain": _domain_from_url(company_website),
        "company_social_links": {},
        "company_summary": _api_company_summary(payload),
        "company_tags": [],
        "company_stats": company_profile.get("stats") or {},
        "company_profile": company_profile,
        "skills": _api_skills(payload),
        "skills_more_count": None,
        "workplace": location,
        "perks_and_benefits": [],
        "certifications": [],
        "videos": [],
        "salary": _api_salary(payload),
        "experience_level": payload.get("experience_level"),
        "profession": payload.get("profession"),
    }

    external_key = application_url or f"{company}|{title}|{location or ''}"
    return RawJob(
        external_id=make_external_id(WTTJ_SOURCE, external_key),
        title=title,
        company=company,
        location=location,
        contract_type=contract_type,
        remote_policy=remote_policy,
        description=description,
        experience=_api_experience(payload),
        application_url=application_url,
        published_date=published_date,
        source=WTTJ_SOURCE,
        source_data=source_data,
    )


def parse_saved_detail(path: str | Path, *, url: str | None = None) -> RawJob:
    """Parse a saved WTTJ job detail HTML file."""
    return parse_detail_html(Path(path).read_text(encoding="utf-8", errors="replace"), url=url)


def _detail_api_url_from_job_url(url: str | None) -> str | None:
    parsed = urlparse(url or "")
    match = re.fullmatch(r"/(?:fr|en)/companies/([^/]+)/jobs/([^/?#]+)", parsed.path)
    if not match:
        return None
    company_slug, job_slug = match.groups()
    return f"{WTTJ_ORGANIZATIONS_API_URL}/{company_slug}/jobs/{job_slug}"


def _api_job_description(payload: dict[str, Any]) -> str:
    sections = [
        ("Description", payload.get("description")),
        ("Missions", payload.get("key_missions")),
        ("Profil recherché", payload.get("looking_for_candidate_description")),
        ("Process de recrutement", payload.get("recruitment_process")),
    ]
    parts: list[str] = []
    for heading, value in sections:
        text = _api_rich_text(value)
        if text:
            parts.append(f"{heading}\n{text}")
    return _clean_text("\n\n".join(parts)) or _api_company_summary(payload) or ""


def _api_rich_text(value: Any) -> str:
    if isinstance(value, list):
        texts = [_api_rich_text(item) for item in value]
        return _clean_text("\n".join(text for text in texts if text))
    if isinstance(value, dict):
        for key in ("description", "content", "name", "title"):
            text = _api_rich_text(value.get(key))
            if text:
                return text
        return ""
    return _description_to_text(_as_text(value))


def _api_skills(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("skills", "tools"):
        raw_items = payload.get(key)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            text = None
            if isinstance(item, dict):
                text = _as_text(item.get("name") or item.get("title") or item.get("label"))
            else:
                text = _as_text(item)
            if text and text not in values:
                values.append(text)
    return values


def _api_salary(payload: dict[str, Any]) -> dict[str, Any]:
    salary = {
        "min": payload.get("salary_min"),
        "max": payload.get("salary_max"),
        "currency": payload.get("salary_currency"),
        "period": payload.get("salary_period"),
    }
    return {key: value for key, value in salary.items() if value is not None}


def _api_experience(payload: dict[str, Any]) -> dict[str, Any] | None:
    level = _as_text(payload.get("experience_level"))
    if not level:
        return None
    min_years = _experience_min_years_from_api_level(level)
    result: dict[str, Any] = {"level": level}
    if min_years is not None:
        result["min_years"] = min_years
        result["required"] = min_years > 0
    return result


def _metadata_text(soup: BeautifulSoup) -> str:
    metadata = soup.select_one('[data-testid="job-metadata-block"]')
    if metadata:
        return _clean_text(metadata.get_text(" ", strip=True))
    return ""


def _skills(soup: BeautifulSoup) -> list[str]:
    block = _block_after_heading(soup, "Compétences & expertises", parent_steps=3)
    if not block:
        return []
    skills: list[str] = []
    for text in _leaf_texts(block, tags=("div", "span")):
        if (
            not text
            or text == "Compétences & expertises"
            or text.startswith("+")
            or text in skills
            or len(text) > 80
        ):
            continue
        skills.append(text)
    return skills


def _skills_more_count(soup: BeautifulSoup) -> int | None:
    block = _block_after_heading(soup, "Compétences & expertises", parent_steps=3)
    if not block:
        return None
    for text in _leaf_texts(block):
        match = re.fullmatch(r"\+(\d+)", text)
        if match:
            return int(match.group(1))
    return None


def _contract_type(
    metadata: str,
    faq: dict[str, str],
    job_posting: dict[str, Any],
) -> str | None:
    combined = " ".join([metadata, *faq.values()])
    for label in CONTRACT_LABELS:
        if re.search(rf"\b{re.escape(label)}\b", combined, flags=re.IGNORECASE):
            return label
    return _normalize_api_contract_type(job_posting.get("employmentType"))


def _remote_text(metadata: str, faq: dict[str, str]) -> str | None:
    match = re.search(
        r"Télétravail\s+(?:total|fréquent|frequent|occasionnel|autorisé|autorise|possible|partiel)",
        metadata,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_text(match.group(0))
    for question, answer in faq.items():
        if "télétravail" in question.lower():
            return answer
    return None
