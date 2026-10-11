"""Welcome to the Jungle metadata blocks for analyzer prompts."""

from __future__ import annotations

from ast import literal_eval
from typing import Any

from smartapply.config import get_settings
from smartapply.filtering.source_fact_builders.wttj import build_wttj_filter_facts
from smartapply.offers.source_metadata_builders.common import (
    _MAX_LIST_ITEMS,
    _MAX_URLS,
    _add_url,
    _append_scalar,
    _compact_mapping,
    _dict,
    _short,
)


def build_wttj_source_metadata(
    source_data: dict[str, Any] | None,
    *,
    fields: set[str] | None = None,
) -> str:
    """Build a compact WTTJ metadata block for analyzer prompts."""
    if not isinstance(source_data, dict):
        return ""

    enabled = fields or _wttj_metadata_fields_from_settings()
    url_lines = _wttj_application_url_lines(source_data, enabled)
    fact_lines = _wttj_fact_lines(source_data, enabled)
    if not url_lines and not fact_lines:
        return ""

    sections: list[str] = []
    if url_lines:
        sections.append("APPLICATION_URL_METADATA:\n" + "\n".join(url_lines))
    if fact_lines:
        sections.append("STRUCTURED_JOB_FACTS:\n" + "\n".join(fact_lines))
    return "\n\n".join(sections)


def _wttj_application_url_lines(source_data: dict[str, Any], enabled: set[str]) -> list[str]:
    lines = ["source: welcometothejungle"]
    detail_api = _dict(source_data.get("detail_api"))
    if "company_domain" in enabled:
        _append_scalar(lines, "company_domain", source_data.get("company_domain"))
    if "company_profile_url" in enabled:
        _append_scalar(lines, "company_profile_url", source_data.get("company_profile_url"))
    if "apply_url" in enabled:
        _append_scalar(lines, "detail_api.ats", detail_api.get("ats"))

    urls: list[dict[str, str]] = []
    if "company_website" in enabled:
        _add_url(urls, source_data.get("company_website"), "company_website")
    if "apply_url" in enabled:
        _add_url(urls, detail_api.get("apply_url"), "detail_api.apply_url")
    for item in urls[:_MAX_URLS]:
        line = (
            f"url: source_field={item['source_field']} | url={item['url']} | "
            f"domain={item['domain']} | url_kind={item['url_kind']}"
        )
        if item.get("company_domain_candidate"):
            line += f" | company_domain_candidate={item['company_domain_candidate']}"
        lines.append(line)
    return lines


def _wttj_fact_lines(source_data: dict[str, Any], enabled: set[str]) -> list[str]:
    lines: list[str] = []
    matches_api = _dict(source_data.get("matches_api"))
    detail_api = _dict(source_data.get("detail_api"))
    company_profile = _dict(source_data.get("company_profile"))

    if "contract_type" in enabled:
        refresh = _dict(source_data.get("_smartapply_wttj_refresh"))
        if refresh.get("schema_version") == 1 and refresh.get("raw_contract_type"):
            facts = build_wttj_filter_facts(source_data)
            _append_scalar(lines, "contract_type", facts.structured_contract_type)
            _append_scalar(lines, "contract_type_source", facts.structured_contract_source)
        else:
            _append_scalar(lines, "matches_api.contract_type", matches_api.get("contract_type"))
            _append_scalar(lines, "employment_type", source_data.get("employment_type"))
    if "remote" in enabled:
        _append_scalar(lines, "matches_api.remote", matches_api.get("remote"))
        _append_scalar(lines, "remote_text", source_data.get("remote_text"))
    if "published_at" in enabled:
        _append_scalar(lines, "matches_api.published_at", matches_api.get("published_at"))
        _append_scalar(lines, "valid_through", source_data.get("valid_through"))
    if "experience_level" in enabled:
        _append_scalar(lines, "experience_level", source_data.get("experience_level"))
        _append_scalar(lines, "matches_api.experience_min", matches_api.get("experience_min"))
        _append_scalar(lines, "matches_api.experience_max", matches_api.get("experience_max"))
    if "salary" in enabled:
        _append_scalar(lines, "salary", _compact_mapping(source_data.get("salary")))
    if "workplace" in enabled:
        _append_scalar(lines, "workplace", source_data.get("workplace"))
    if "skills" in enabled:
        fallback_skills = (
            None
            if detail_api.get("skills") or detail_api.get("tools")
            else source_data.get("skills")
        )
        _append_scalar(lines, "skills", _wttj_names(detail_api.get("skills"), fallback_skills))
        _append_scalar(lines, "tools", _wttj_names(detail_api.get("tools")))
    if "profession" in enabled:
        _append_scalar(lines, "profession", _wttj_profession_summary(source_data.get("profession")))
    if "sectors" in enabled:
        _append_scalar(lines, "company_profile.sectors", company_profile.get("sectors"))
    if "offices" in enabled:
        _append_scalar(lines, "company_profile.offices", company_profile.get("offices"))
    if "company_stats" in enabled:
        _append_scalar(
            lines, "company_profile.stats", _compact_mapping(company_profile.get("stats"))
        )
    if "company_summary" in enabled:
        _append_scalar(lines, "company_summary", source_data.get("company_summary"))
    if "company_presentation" in enabled:
        _append_scalar(lines, "company_profile.presentation", company_profile.get("presentation"))
    return lines


def _wttj_names(*values: Any) -> str:
    names: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            name = _wttj_item_name(item)
            if name and name not in names:
                names.append(name)
    return "; ".join(names[:_MAX_LIST_ITEMS])


def _wttj_item_name(item: Any) -> str:
    if isinstance(item, dict):
        return _wttj_localized_text(item.get("name") or item.get("title") or item.get("label"))
    return _wttj_localized_text(item)


def _wttj_localized_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("fr", "en"):
            text = _short(value.get(key))
            if text:
                return text
        for raw in value.values():
            text = _short(raw)
            if text:
                return text
        return ""
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = literal_eval(text)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, dict):
            return _wttj_localized_text(parsed)
    return _short(text)


def _wttj_profession_summary(value: Any) -> str:
    data = _dict(value)
    parts = [
        _wttj_localized_text(data.get("category_name")),
        _wttj_localized_text(data.get("sub_category_name")),
    ]
    return " / ".join(part for part in parts if part)


def _wttj_metadata_fields_from_settings() -> set[str]:
    raw = get_settings().wttj_analyzer_metadata_fields
    return {field.strip() for field in raw.split(",") if field.strip()}
