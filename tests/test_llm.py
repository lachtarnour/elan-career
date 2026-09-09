"""Tests for the LLM module — schemas, mock provider, cache key, usage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from smartapply.llm import (
    JobAnalysis,
    MockLLMProvider,
    get_llm_provider,
    make_cache_key,
)
from smartapply.llm.prompts import job_analysis
from smartapply.offers import AnalyzerInput, build_analyzer_input
from smartapply.offers.source_metadata_builders import (
    build_francetravail_source_metadata,
    build_linkedin_source_metadata,
    build_serpapi_source_metadata,
    build_wttj_source_metadata,
)

# ---------------- Schemas ----------------


def test_job_analysis_schema_round_trip() -> None:
    a = JobAnalysis(
        role_type="Data Scientist NLP",
        seniority="mid",
        domain="HealthTech",
        main_tasks=["Build pipelines"],
        required_skills=["Python", "PyTorch"],
        nice_to_have=["AWS"],
        match_reasons=["Strong NLP background"],
        risks=["No prod deploy experience"],
        cv_keywords_to_include=["PyTorch", "RAG"],
        extracted_location="Paris",
        company_context="Acme builds clinical AI products for hospital teams.",
        offer_interest_points=[
            "Improve medical knowledge access with RAG",
            "Work with product and clinical stakeholders",
        ],
    )
    raw = a.model_dump_json()
    assert '"fit_score":0.5' in raw
    assert "Data Scientist" in raw
    assert "clinical AI products" in raw
    assert "Paris" in raw
    b = JobAnalysis.model_validate_json(raw)
    assert b == a


def test_job_analysis_prompt_includes_structured_location() -> None:
    from smartapply.profile import get_profile

    prompt = job_analysis.build_user_prompt(
        profile=get_profile(),
        job_title="Data Scientist",
        job_company="Acme",
        job_location="France",
        application_url="https://acme.ai/jobs/42",
        job_description="Poste base a Paris, rythme hybride.",
    )

    assert (
        "Structured location (metadata only; do not copy into extracted_location): France" in prompt
    )
    assert "Poste base a Paris" in prompt
    assert "extracted_location" in job_analysis.SYSTEM


def _metadata_line(metadata: str, source_field: str) -> str:
    for line in metadata.splitlines():
        if f"source_field={source_field}" in line:
            return line
    raise AssertionError(f"Missing metadata URL line for {source_field}")


def test_francetravail_source_metadata_classifies_application_urls() -> None:
    metadata = build_francetravail_source_metadata(
        {
            "id": "206GJTL",
            "entreprise": {
                "nom": "CIRIL GROUP",
                "url": "https://www.cirilgroup.com/fr/recrutement.html",
                "description": "Long company description already included elsewhere.",
            },
            "origineOffre": {
                "origine": 1,
                "urlOrigine": "https://candidat.francetravail.fr/offres/recherche/detail/206GJTL",
                "partenaires": [
                    {"nom": "Broadbean", "url": "https://www.aplitrak.com/?adid=abc"},
                ],
            },
            "contact": {
                "nom": "Service recrutement",
                "courriel": "jobs@cirilgroup.com",
                "urlPostulation": "https://taleez.com/apply/data-engineer/applying",
                "coordonnees1": "Postuler ici: https://candidat.francetravail.fr/offres/recherche/detail/206GJTL",
            },
        }
    )

    assert "APPLICATION_URL_METADATA" in metadata
    assert "source: francetravail" in metadata
    assert "raw_id: 206GJTL" in metadata
    assert "entreprise.nom: CIRIL GROUP" in metadata
    assert "contact.nom: Service recrutement" in metadata
    assert "contact.courriel: jobs@cirilgroup.com" in metadata

    company = _metadata_line(metadata, "entreprise.url")
    assert "domain=cirilgroup.com" in company
    assert "url_kind=company_url" in company
    assert "company_domain_candidate=cirilgroup.com" in company

    postulation = _metadata_line(metadata, "contact.urlPostulation")
    assert "domain=taleez.com" in postulation
    assert "url_kind=ats" in postulation
    assert "company_domain_candidate" not in postulation

    coordonnees = _metadata_line(metadata, "contact.coordonnees1")
    assert "domain=francetravail.fr" in coordonnees
    assert "url_kind=francetravail" in coordonnees
    assert "company_domain_candidate" not in coordonnees

    partner = _metadata_line(metadata, "origineOffre.partenaires[0].url")
    assert "domain=aplitrak.com" in partner
    assert "url_kind=ats" in partner
    assert "company_domain_candidate" not in partner

    origin = _metadata_line(metadata, "origineOffre.urlOrigine")
    assert "url_kind=francetravail" in origin


def test_wttj_source_metadata_includes_application_and_company_facts() -> None:
    metadata = build_wttj_source_metadata(
        {
            "company_website": "https://www.phagos.org",
            "company_domain": "phagos.org",
            "company_profile_url": "https://www.welcometothejungle.com/fr/companies/phagos-1",
            "company_summary": "Phagos développe des thérapies contre les infections bactériennes.",
            "workplace": "Suresnes, France",
            "skills": ["Python", "SQL", "Machine Learning"],
            "employment_type": "FULL_TIME",
            "remote_text": "Télétravail fréquent",
            "valid_through": "2026-08-25T22:01:01.000Z",
            "experience_level": "3_TO_4_YEARS",
            "profession": {
                "category_name": {"fr": "Technologie et ingénierie", "en": "Tech & Engineering"},
                "sub_category_name": {
                    "fr": "Données/Business Intelligence",
                    "en": "Data / Business Intelligence",
                },
            },
            "salary": {"min": 45000, "max": 50000, "currency": "EUR", "period": "yearly"},
            "detail_api": {
                "apply_url": "https://phagos.welcomekit.co/jobs/business-data",
                "ats": "wkit",
                "skills": [
                    {
                        "name": {
                            "fr": "Communication",
                            "en": "Communication skills",
                        }
                    }
                ],
                "tools": [{"name": "Python"}, {"name": "SQL"}],
            },
            "matches_api": {
                "contract_type": "full_time",
                "remote": "partial",
                "published_at": "2026-06-01T10:00:00Z",
                "experience_min": 3.0,
                "experience_max": 4.0,
            },
            "company_profile": {
                "sectors": "Intelligence artificielle / Machine Learning, Santé",
                "offices": "Paris",
                "stats": {"employees": "50", "founded": "2021"},
                "presentation": "Phagos travaille sur les phages.",
            },
        }
    )

    assert "APPLICATION_URL_METADATA" in metadata
    assert "source: welcometothejungle" in metadata
    assert "company_domain: phagos.org" in metadata
    assert "detail_api.ats: wkit" in metadata
    assert "source_field=company_website" in metadata
    assert "domain=phagos.org" in metadata
    assert "url_kind=company_url" in metadata
    assert "source_field=detail_api.apply_url" in metadata
    assert "domain=welcomekit.co" in metadata
    assert "STRUCTURED_JOB_FACTS" in metadata
    assert "matches_api.contract_type: full_time" in metadata
    assert "matches_api.remote: partial" in metadata
    assert "experience_level: 3_TO_4_YEARS" in metadata
    assert "matches_api.experience_min: 3.0" in metadata
    assert "matches_api.experience_max: 4.0" in metadata
    assert "salary: currency=EUR; max=50000; min=45000; period=yearly" in metadata
    assert "workplace: Suresnes, France" in metadata
    assert "skills: Communication" in metadata
    assert "tools: Python; SQL" in metadata
    assert "profession: Technologie et ingénierie / Données/Business Intelligence" in metadata
    assert (
        "company_profile.sectors: Intelligence artificielle / Machine Learning, Santé" in metadata
    )
    assert "company_profile.stats: employees=50; founded=2021" in metadata
    assert '"matches_api"' not in metadata


def test_serpapi_source_metadata_includes_compact_source_signals() -> None:
    metadata = build_serpapi_source_metadata(
        {
            "title": "Data Scientist Expert IA & Machine Learning H/F",
            "company_name": "StarClay",
            "location": "Paris, France",
            "share_link": "https://www.google.com/search?q=jobs",
            "apply_options": [
                {"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/123"},
                {"title": "Company site", "link": "https://jobs.starclay.fr/apply"},
            ],
            "detected_extensions": {
                "schedule_type": "Full-time",
                "work_from_home": True,
                "posted_at": "3 days ago",
            },
            "_smartapply_search": {
                "result_origin": "fallback",
                "strict_chips": "date_posted:week",
                "fallback_reason": "low_result_strict_filters",
            },
            "job_highlights": [
                {
                    "title": "Responsibilities",
                    "items": [
                        "Build NLP and computer vision models.",
                        "Monitor and retrain machine-learning systems.",
                    ],
                },
                {
                    "title": "Qualifications",
                    "items": ["Python", "SQL", "MLOps"],
                },
            ],
        }
    )

    assert "APPLICATION_URL_METADATA" in metadata
    assert "source: serpapi" in metadata
    assert "company_name: StarClay" in metadata
    assert "apply_options: LinkedIn; Company site" in metadata
    assert "source_field=apply_options[0].link" in metadata
    assert "domain=linkedin.com" in metadata
    assert "url_kind=partner_job_board" in metadata
    assert "source_field=apply_options[1].link" in metadata
    assert "company_domain_candidate=starclay.fr" in metadata
    assert "search.result_origin: fallback" in metadata
    assert "STRUCTURED_JOB_FACTS" in metadata
    assert "detected_extensions.schedule_type: Full-time" in metadata
    assert "detected_extensions.work_from_home: True" in metadata
    assert "job_highlights.Responsibilities: Build NLP and computer vision models." in metadata
    assert "MOTIVATION_ANCHORS" in metadata
    assert '"job_highlights"' not in metadata
    assert "{'title'" not in metadata


def test_linkedin_source_metadata_includes_api_signals_without_raw_html() -> None:
    metadata = build_linkedin_source_metadata(
        {
            "id": 1234567890,
            "url": "[https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890](https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890)",
            "title": "Data Scientist- Paris",
            "location": "Paris, Île-de-France, France",
            "postedDate": "2026-06-30T00:00:00.000Z",
            "companyName": "Example Energy",
            "companyUrl": "[https://cn.linkedin.com/company/example-energy](https://cn.linkedin.com/company/example-energy)",
            "recruiterName": "",
            "recruiterUrl": "",
            "experienceLevel": "Mid-Senior level",
            "contractType": "Full-time",
            "workType": "Project Management",
            "sector": "Energy Technology",
            "salary": "",
            "applyType": "EASY_APPLY",
            "postedTimeAgo": "18 hours ago",
            "applicationsCount": "Over 200 applicants",
            "descriptionHtml": "<p>Do not include raw HTML in metadata.</p>",
            "applyUrl": "",
            "_smartapply_normalized": {
                "url": "https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890",
                "companyUrl": "https://cn.linkedin.com/company/example-energy",
                "description_source": "descriptionHtml",
            },
            "_smartapply_search": {
                "title": "Data Scientist",
                "location": "France",
                "datePosted": "r86400",
                "contractType": ["F"],
                "experienceLevel": ["4"],
                "remote": ["1", "2", "3"],
                "experience_pass": 2,
                "experience_fallback_used": True,
            },
        }
    )

    assert "APPLICATION_URL_METADATA" in metadata
    assert "source: linkedin" in metadata
    assert "companyName: Example Energy" in metadata
    assert "applyType: EASY_APPLY" in metadata
    assert "source_field=url" in metadata
    assert "domain=linkedin.com" in metadata
    assert "source_field=companyUrl" in metadata
    assert "STRUCTURED_JOB_FACTS" in metadata
    assert "experienceLevel: Mid-Senior level" in metadata
    assert "search.experience_fallback_used: True" in metadata
    assert "normalized.description_source: descriptionHtml" in metadata
    assert "MOTIVATION_ANCHORS" in metadata
    assert "sector: Energy Technology" in metadata
    assert "Over 200 applicants" in metadata
    assert "<p>" not in metadata
    assert '"descriptionHtml"' not in metadata


def test_build_analyzer_input_uses_linkedin_offer_adapter() -> None:
    job = SimpleNamespace(
        title="Data Scientist- Paris",
        company="Example Energy",
        location="Paris, Île-de-France, France",
        application_url="https://fr.linkedin.com/jobs/view/1234567890",
        cleaned_description="Analyze raw data.\nBuild Python ML models.",
        description="Raw fallback.",
        source="linkedin",
        source_data={
            "companyName": "Example Energy",
            "applyType": "EASY_APPLY",
            "experienceLevel": "Mid-Senior level",
            "contractType": "Full-time",
            "_smartapply_normalized": {"description_source": "descriptionHtml"},
        },
    )

    analyzer_input = build_analyzer_input(job)

    assert analyzer_input.offer_body == "Analyze raw data.\nBuild Python ML models."
    assert analyzer_input.source == "linkedin"
    assert "source: linkedin" in analyzer_input.source_metadata
    assert "applyType: EASY_APPLY" in analyzer_input.source_metadata
    assert "normalized.description_source: descriptionHtml" in analyzer_input.source_metadata


def test_build_analyzer_input_uses_manual_offer_body_without_legacy_metadata() -> None:
    job = SimpleNamespace(
        title="Data Scientist NLP",
        company="Acme",
        location=None,
        application_url=None,
        cleaned_description="Missions: build RAG pipelines with Python.",
        description="Missions: build RAG pipelines with Python.",
        source="manual",
        source_data={"input": "text"},
    )

    analyzer_input = build_analyzer_input(job)

    assert analyzer_input == AnalyzerInput(
        title="Data Scientist NLP",
        company="Acme",
        location=None,
        application_url=None,
        offer_body="Missions: build RAG pipelines with Python.",
        source="manual",
        source_metadata="",
    )


# ---------------- Cache key ----------------


def test_cache_key_is_deterministic_and_collision_resistant() -> None:
    k1 = make_cache_key(
        model="gpt-4o-mini",
        system="s",
        user="u",
        schema_name="JobAnalysis",
        extra={"temperature": 0.2},
    )
    k2 = make_cache_key(
        model="gpt-4o-mini",
        system="s",
        user="u",
        schema_name="JobAnalysis",
        extra={"temperature": 0.2},
    )
    k3 = make_cache_key(
        model="gpt-4o-mini",
        system="s",
        user="u DIFFERENT",
        schema_name="JobAnalysis",
        extra={"temperature": 0.2},
    )
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64  # sha256 hex


# ---------------- Usage / pricing ----------------


class _TinyAnswer(BaseModel):
    value: str


def test_gpt54_pricing_accounts_for_provider_cached_tokens() -> None:
    from smartapply.llm.usage import estimate_cost_usd

    cost = estimate_cost_usd(
        "gpt-5.4-2026-03-05",
        prompt_tokens=1_000,
        cached_prompt_tokens=400,
        completion_tokens=100,
    )

    assert cost == pytest.approx((600 * 2.50 + 400 * 0.25 + 100 * 15.00) / 1_000_000)


def test_gpt56_pricing_accounts_for_cache_write_surcharge() -> None:
    from smartapply.llm.usage import estimate_cost_usd

    cost = estimate_cost_usd(
        "gpt-5.6-terra",
        prompt_tokens=1_000,
        cached_prompt_tokens=400,
        cache_write_prompt_tokens=200,
        completion_tokens=100,
    )

    assert cost == pytest.approx(
        (400 * 2.00 + 400 * 0.20 + 200 * (2.00 * 1.25) + 100 * 12.00) / 1_000_000
    )


def test_openai_usage_is_recorded_without_exact_cache_and_uses_cached_rate(
    isolated_db,
    monkeypatch,
) -> None:
    from sqlalchemy import select

    from smartapply.database import session_scope
    from smartapply.database.models import LLMUsage
    from smartapply.llm.openai_provider import OpenAIProvider

    captured: dict = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"ok"}'))],
        usage=SimpleNamespace(
            prompt_tokens=1_000,
            completion_tokens=100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=400),
        ),
    )
    provider = OpenAIProvider()

    def fake_call(**kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(provider, "_call_openai", fake_call)

    result = provider.complete_json(
        system="stable instructions",
        user="job-specific input",
        schema=_TinyAnswer,
        model="gpt-5.4",
        purpose="cost_test",
        use_cache=False,
    )

    assert result.value == "ok"
    assert captured["prompt_cache_key"] == "elan:cost_test:_TinyAnswer"
    assert captured["max_completion_tokens"] == 6000
    with session_scope() as session:
        usage = session.scalar(select(LLMUsage))
        assert usage is not None
        assert usage.cached is False
        assert usage.cached_prompt_tokens == 400
        assert usage.cost_usd == pytest.approx((600 * 2.50 + 400 * 0.25 + 100 * 15.00) / 1_000_000)


def test_invalid_exact_cache_is_replaced_once_instead_of_repeatedly_paid(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.database import session_scope
    from smartapply.database.repository import cache_set
    from smartapply.llm.openai_provider import OpenAIProvider

    cache_key = make_cache_key(
        model="gpt-5.4",
        system="stable",
        user="dynamic",
        schema_name=_TinyAnswer.__name__,
        extra={
            "temperature": 0.2,
            "schema": _TinyAnswer.model_json_schema(),
        },
    )
    with session_scope() as session:
        cache_set(
            session,
            cache_key=cache_key,
            model="gpt-5.4",
            response="{}",
            prompt_tokens=10,
            completion_tokens=2,
            purpose="repair_test",
        )

    calls = 0
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"fresh"}'))],
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=3,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )
    provider = OpenAIProvider()

    def fake_call(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        return response

    monkeypatch.setattr(provider, "_call_openai", fake_call)

    first = provider.complete_json(
        system="stable",
        user="dynamic",
        schema=_TinyAnswer,
        model="gpt-5.4",
        purpose="repair_test",
    )
    second = provider.complete_json(
        system="stable",
        user="dynamic",
        schema=_TinyAnswer,
        model="gpt-5.4",
        purpose="repair_test",
    )

    assert first.value == second.value == "fresh"
    assert calls == 1


def test_refresh_cache_bypasses_old_response_and_replaces_it(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.database import session_scope
    from smartapply.database.repository import cache_set
    from smartapply.llm.openai_provider import OpenAIProvider

    cache_key = make_cache_key(
        model="gpt-5.4",
        system="stable",
        user="dynamic",
        schema_name=_TinyAnswer.__name__,
        extra={
            "temperature": 0.2,
            "schema": _TinyAnswer.model_json_schema(),
        },
    )
    with session_scope() as session:
        cache_set(
            session,
            cache_key=cache_key,
            model="gpt-5.4",
            response='{"value":"old"}',
            prompt_tokens=10,
            completion_tokens=2,
            purpose="refresh_test",
        )

    calls = 0
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"fresh"}'))],
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=3,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )
    provider = OpenAIProvider()

    def fake_call(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        return response

    monkeypatch.setattr(provider, "_call_openai", fake_call)

    refreshed = provider.complete_json(
        system="stable",
        user="dynamic",
        schema=_TinyAnswer,
        model="gpt-5.4",
        purpose="refresh_test",
        refresh_cache=True,
    )
    reused = provider.complete_json(
        system="stable",
        user="dynamic",
        schema=_TinyAnswer,
        model="gpt-5.4",
        purpose="refresh_test",
    )

    assert refreshed.value == reused.value == "fresh"
    assert calls == 1


def test_purge_expired_cache_deletes_only_entries_older_than_15_days(
    isolated_db,
) -> None:
    from smartapply.database import session_scope
    from smartapply.database.repository import cache_get, cache_set, purge_expired_cache

    reference = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        expired = cache_set(
            session,
            cache_key="a" * 64,
            model="gpt-5.4-mini",
            response='{"value":"expired"}',
            prompt_tokens=10,
            completion_tokens=2,
            purpose="expiry_test",
        )
        active = cache_set(
            session,
            cache_key="b" * 64,
            model="gpt-5.4-mini",
            response='{"value":"active"}',
            prompt_tokens=10,
            completion_tokens=2,
            purpose="expiry_test",
        )
        expired.created_at = reference - timedelta(days=16)
        active.created_at = reference - timedelta(days=14)
        session.flush()

        deleted = purge_expired_cache(session, ttl_days=15, now=reference)

        assert deleted == 1
        assert cache_get(session, "a" * 64) is None
        assert cache_get(session, "b" * 64) is not None


def test_cache_hit_telemetry_failure_never_triggers_a_paid_call(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.database import session_scope
    from smartapply.database.repository import cache_set
    from smartapply.llm.openai_provider import OpenAIProvider

    cache_key = make_cache_key(
        model="gpt-5.4",
        system="stable",
        user="dynamic",
        schema_name=_TinyAnswer.__name__,
        extra={
            "temperature": 0.2,
            "schema": _TinyAnswer.model_json_schema(),
        },
    )
    with session_scope() as session:
        cache_set(
            session,
            cache_key=cache_key,
            model="gpt-5.4",
            response='{"value":"cached"}',
            prompt_tokens=10,
            completion_tokens=2,
            purpose="telemetry_test",
        )

    provider = OpenAIProvider()

    def fail_usage(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("telemetry unavailable")

    def fail_paid_call(**kwargs):  # noqa: ARG001
        raise AssertionError("valid cache hit reached the paid provider")

    monkeypatch.setattr("smartapply.llm.openai_provider.record_usage", fail_usage)
    monkeypatch.setattr(provider, "_call_openai", fail_paid_call)

    result = provider.complete_json(
        system="stable",
        user="dynamic",
        schema=_TinyAnswer,
        model="gpt-5.4",
        purpose="telemetry_test",
    )

    assert result.value == "cached"


# ---------------- Mock provider ----------------


# ---------------- Factory ----------------


def test_factory_returns_mock_by_default() -> None:
    p = get_llm_provider()
    assert isinstance(p, MockLLMProvider)
    assert p.smart_model.startswith("mock") and p.cheap_model.startswith("mock")
