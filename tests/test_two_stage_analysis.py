"""Offer extraction must remain independent of the profile and matching output."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.models import Job, JobScore, JobStatus, LLMCache
from smartapply.database.repository.llm_cache import purge_expired_cache
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.llm import JobAnalysis, JobFitAssessment, LLMError, OfferAnalysis, OpenAIProvider
from smartapply.llm.prompts import job_analysis, job_fit
from smartapply.pipeline.errors import StaleTaskResultError
from smartapply.pipeline.process.analysis import (
    AnalysisMixin,
    _assemble_analysis,
    _ground_offer_excerpts,
)
from smartapply.pipeline.rescore import reusable_offer
from smartapply.profile import get_profile
from tests.fit_fixtures import evaluation_from_user

BODY = "Build forecasting models. Python is required. SQL is preferred."


def offer_result():
    return OfferAnalysis(
        role_type="Data Scientist",
        seniority="",
        domain="",
        main_tasks=["Build forecasting models"],
        required_skills=["Python"],
        nice_to_have=["SQL"],
        cv_keywords_to_include=["forecasting models"],
        supporting_excerpts=["Build forecasting models.", "Python is required."],
        offer_risks=["Working location is not stated in the body."],
    )


def fit_result():
    return JobFitAssessment(
        fit_score=0.72,
        match_reasons=["Declared Python and documented modelling work support the core task."],
        risks=["Forecasting depth is not fully documented."],
    )


def runner(provider, profile=None):
    instance = AnalysisMixin()
    instance.profile = profile or get_profile().model_copy(deep=True)
    instance.llm = provider
    instance.settings = get_settings()
    return instance


def insert_job():
    with session_scope() as session:
        now = datetime.now(timezone.utc)
        job = Job(
            external_id="two-stage-offer",
            title="Data Scientist",
            company="Example",
            location="Metadata City",
            description=BODY,
            source="manual",
            filtered_at=now,
            ranked_at=now,
            score=JobScore(
                final_score=0.7,
                components={
                    "filter_disposition": "pending_relevance",
                    "hard_reject": HardRejectResult(decisions=()).audit_payload(),
                },
            ),
        )
        session.add(job)
        session.flush()
        return job.id


@pytest.mark.parametrize(
    "components",
    [
        {},
        {"filter_disposition": "relevant"},
        {"filter_disposition": "uncertain"},
        {"filter_disposition": "pending_relevance"},
        {
            "filter_disposition": "pending_relevance",
            "hard_reject": {
                "ruleset_version": "old-filter",
                "rejected": False,
                "gates": [],
            },
        },
        {
            "filter_disposition": "rejected",
            "hard_reject": {
                "ruleset_version": "hard-reject-v2",
                "rejected": True,
                "gates": [],
            },
        },
    ],
)
def test_analysis_requires_a_current_kept_filter_decision(isolated_db, components):
    job_id = insert_job()
    with session_scope() as session:
        session.get(JobScore, job_id).components = components
    calls = []
    instance = runner(
        SimpleNamespace(
            complete_json=lambda **kwargs: calls.append(kwargs["purpose"]),
            cheap_model="mock",
        )
    )

    assert instance._analyze_one(job_id=job_id) is False
    report = instance.analyze_jobs([job_id])
    assert report.analyzed == 0 and report.skipped_missing == 1
    assert calls == []
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.analysis is None and job.analyzed_at is None
        assert job.score.components == components
        assert job.score.final_score == 0.7


@pytest.mark.parametrize("changed_purpose", ["job_offer_analysis", "job_fit_assessment"])
def test_rejection_during_analysis_stops_remaining_calls_and_publication(
    isolated_db,
    changed_purpose,
):
    job_id = insert_job()
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == changed_purpose:
            with session_scope() as session:
                session.get(JobScore, job_id).components = {
                    "filter_disposition": "rejected",
                    "hard_reject": {
                        "ruleset_version": "hard-reject-v2",
                        "rejected": True,
                        "gates": [],
                    },
                }
        return (
            offer_result()
            if kwargs["purpose"] == "job_offer_analysis"
            else evaluation_from_user(kwargs["user"])
        )

    instance = runner(SimpleNamespace(complete_json=complete, cheap_model="mock"))
    with pytest.raises(StaleTaskResultError):
        instance._analyze_one(job_id=job_id)

    assert calls == (
        ["job_offer_analysis"]
        if changed_purpose == "job_offer_analysis"
        else ["job_offer_analysis", "job_fit_assessment"]
    )
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is None


def test_extraction_is_identical_for_different_profiles():
    original = get_profile().model_copy(deep=True)
    changed = original.model_copy(deep=True)
    changed.identity.summary = "PRIVATE_CANDIDATE_FACT"
    changed.skills.categories[0].skills.append("PRIVATE_NEW_SKILL")
    changed.experiences[0].bullets[0].text = "PRIVATE_EXPERIENCE"
    first = job_analysis.build_user_prompt(profile=original, job_title="Role", job_description=BODY)
    second = job_analysis.build_user_prompt(profile=changed, job_title="Role", job_description=BODY)
    assert first == second
    for private in (
        "PRIVATE_CANDIDATE_FACT",
        "PRIVATE_NEW_SKILL",
        "PRIVATE_EXPERIENCE",
        original.identity.email,
        original.identity.full_name,
    ):
        assert private not in second


def test_fit_receives_complete_facts_without_cv_policy_or_invented_preferences():
    profile = get_profile().model_copy(deep=True)
    profile.identity.location = "Candidate Residence"
    profile.preferences.target_roles = ["Chosen role"]
    profile.preferences.deal_breakers = ["Explicit personal constraint"]
    profile.projects[-1].selection_policy.tier = "disabled"
    prompt = job_fit.build_user_prompt(
        profile=profile, offer_analysis=offer_result(), reference_date=date(2026, 9, 22)
    )
    candidate = json.loads(
        prompt.split("=== CANDIDATE PROFILE JSON ===\n", 1)[1].split("\n\n===", 1)[0]
    )
    assert candidate["identity"]["residence"] == "Candidate Residence"
    assert candidate["preferences"] == profile.preferences.model_dump(mode="json")
    assert [e["id"] for e in candidate["experiences"]] == [e.id for e in profile.experiences]
    assert [p["id"] for p in candidate["projects"]] == [p.id for p in profile.projects]
    for source, supplied in zip(
        profile.experiences + profile.projects,
        candidate["experiences"] + candidate["projects"],
        strict=True,
    ):
        assert [b["text"] for b in supplied["bullets"]] == [b.text for b in source.bullets]
    assert "selection_policy" not in prompt
    assert "Preferred locations:" not in prompt
    assert "junior to mid (1-3 years)" not in prompt
    assert profile.identity.email not in prompt
    assert "2026-09-22" not in prompt


@pytest.mark.parametrize("field", ["required_skills", "seniority", "constraints", "main_tasks"])
def test_fit_schema_rejects_offer_field_rewrites(field):
    with pytest.raises(ValidationError):
        JobFitAssessment.model_validate({**fit_result().model_dump(), field: []})


def test_new_fit_score_is_required_but_old_analyses_still_load():
    with pytest.raises(ValidationError):
        JobFitAssessment(match_reasons=[], risks=[])
    old = {**offer_result().model_dump(), "match_reasons": [], "risks": []}
    for key in ("constraints", "supporting_excerpts", "offer_risks"):
        old.pop(key)
    historical = JobAnalysis.model_validate(old)
    assert historical.fit_score == 0.5
    assert historical.supporting_excerpts == []
    assert historical.required_skills == ["Python"]


def test_assembly_preserves_extraction_and_both_kinds_of_risk():
    offer, fit = offer_result(), fit_result()
    original_offer, original_fit = offer.model_dump(), fit.model_dump()
    analysis = _assemble_analysis(offer, fit)
    assert {k: analysis.model_dump()[k] for k in OfferAnalysis.model_fields} == original_offer
    assert analysis.fit_score == fit.fit_score
    assert analysis.risks == [*offer.offer_risks, *fit.risks]
    assert offer.model_dump() == original_offer
    assert fit.model_dump() == original_fit


def test_unverified_excerpt_is_removed_and_reported_without_inventing_a_candidate_gap():
    offer = offer_result()
    offer.supporting_excerpts.extend(["Kubernetes is required.", ""])
    grounded = _ground_offer_excerpts(offer, BODY)
    assert grounded.supporting_excerpts == ["Build forecasting models.", "Python is required."]
    assert grounded.required_skills == offer.required_skills
    assert "excluded" in grounded.offer_risks[-1]
    assert "Kubernetes is required." in offer.supporting_excerpts


def test_retained_excerpt_uses_exact_source_whitespace():
    offer = offer_result()
    offer.supporting_excerpts = ["Python is required."]
    body = "Python\n  is required. SQL is preferred."
    grounded = _ground_offer_excerpts(offer, body)
    assert grounded.supporting_excerpts == ["Python\n  is required."]
    assert grounded.supporting_excerpts[0] in body
    assert grounded.offer_risks == offer.offer_risks


@pytest.mark.parametrize(
    "quote",
    [
        '"Python is required."',
        "'Python is required.'",
        "“Python is required.”",
        "‘Python is required.’",
        "« Python is required. »",
        "‹Python is required.›",
        "„Python is required.“",
        ' « “"Python is required."” » ',
    ],
)
def test_paired_quote_wrappers_preserve_exact_source_and_extraction_facts(quote):
    offer = offer_result()
    offer.supporting_excerpts = [quote]
    original = offer.model_dump()
    body = "Python\n  is required. SQL is preferred."
    grounded = _ground_offer_excerpts(offer, body)
    assert grounded.supporting_excerpts == ["Python\n  is required."]
    assert grounded.model_dump(exclude={"supporting_excerpts"}) == offer.model_dump(
        exclude={"supporting_excerpts"}
    )
    assert offer.model_dump() == original


@pytest.mark.parametrize(
    ("quote", "body", "expected"),
    [
        (
            '"Python is required."',
            'Python is required. Then "Python is required."',
            '"Python is required."',
        ),
        ("« Python is required. »", "«\nPython is required.\n»", "«\nPython is required.\n»"),
        (
            '« Build "Python" tools for l’équipe. »',
            'Build "Python" tools for l’équipe.',
            'Build "Python" tools for l’équipe.',
        ),
        (
            '"Develop Python\'s ecosystem."',
            "Develop Python's ecosystem.",
            "Develop Python's ecosystem.",
        ),
    ],
)
def test_source_quotes_and_internal_apostrophes_are_preserved(quote, body, expected):
    offer = offer_result()
    offer.supporting_excerpts = [quote]
    grounded = _ground_offer_excerpts(offer, body)
    assert grounded.supporting_excerpts == [expected]
    assert expected in body
    assert grounded.offer_risks == offer.offer_risks


@pytest.mark.parametrize(
    "quote",
    [
        '"Kubernetes is required."',
        '"python is required."',
        '"Python is required!"',
        '"Python is mandatory."',
        '"Python is required.»',
        '"Python is required.',
        "Python is required.”",
        '""',
        "« »",
        '« "" »',
        "",
        "   ",
    ],
)
def test_wrapper_tolerance_does_not_accept_changed_missing_or_empty_quotes(quote):
    offer = offer_result()
    offer.supporting_excerpts = ["« Python is required. »", quote]
    grounded = _ground_offer_excerpts(offer, BODY)
    assert grounded.supporting_excerpts == ["Python is required."]
    assert grounded.required_skills == offer.required_skills
    assert grounded.offer_risks[:-1] == offer.offer_risks
    assert "excluded" in grounded.offer_risks[-1]


@pytest.mark.parametrize("failed_purpose", ["job_offer_analysis", "job_fit_assessment"])
def test_failure_does_not_save_partial_analysis_or_mark_job_analyzed(isolated_db, failed_purpose):
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == failed_purpose:
            raise LLMError("Simulated controller failure")
        return offer_result()

    instance = runner(SimpleNamespace(complete_json=complete, cheap_model="gpt-5.4-mini"))
    job_id = insert_job()
    with pytest.raises(LLMError):
        instance._analyze_one(job_id=job_id)
    assert len(calls) == (1 if failed_purpose == "job_offer_analysis" else 2)
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.analysis is None
        assert job.analyzed_at is None
        assert job.status == JobStatus.SCRAPED
        assert job.location == "Metadata City"


@pytest.mark.parametrize("retry_after_failure", [False, True])
@pytest.mark.parametrize("wrapped_excerpts", [False, True])
def test_offer_cache_survives_profile_edit_or_failed_fit(
    isolated_db, monkeypatch, retry_after_failure, wrapped_excerpts
):
    """Exercise the real cache/provider boundary; network calls are replaced locally."""
    provider = OpenAIProvider()
    calls = []
    fail_fit_once = retry_after_failure
    extracted = offer_result()
    if wrapped_excerpts:
        extracted.supporting_excerpts = [
            f'« "{quote}" »' for quote in extracted.supporting_excerpts
        ]

    def fake_api(**kwargs):
        nonlocal fail_fit_once
        schema_name = kwargs["response_format"]["json_schema"]["name"]
        calls.append((schema_name, kwargs))
        if schema_name == "JobFitDecisionEvaluation" and fail_fit_once:
            fail_fit_once = False
            raise RuntimeError("Temporary matching failure")
        result = (
            extracted
            if schema_name == "OfferAnalysis"
            else evaluation_from_user(kwargs["messages"][1]["content"])
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=result.model_dump_json()))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(provider, "_call_openai", fake_api)
    instance = runner(provider)
    job_id = insert_job()
    if retry_after_failure:
        with pytest.raises(LLMError):
            instance._analyze_one(job_id=job_id)
    else:
        instance._analyze_one(job_id=job_id)
        assert instance.analyze_jobs([job_id]).already_analyzed == 1
        instance.profile.skills.categories[0].skills.append("NEW_DECLARED_SKILL")
    # An explicit internal rerun can reuse extraction; normal analyze_jobs still
    # skips history and never silently rewrites a previously displayed score.
    instance._analyze_one(job_id=job_id)
    assert [name for name, _ in calls] == [
        "OfferAnalysis",
        "JobFitDecisionEvaluation",
        "JobFitDecisionEvaluation",
    ]
    assert all(kwargs["model"] == "gpt-5.4-mini" for _, kwargs in calls)
    assert "NEW_DECLARED_SKILL" not in calls[0][1]["messages"][1]["content"]
    if not retry_after_failure:
        assert "NEW_DECLARED_SKILL" in calls[-1][1]["messages"][1]["content"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.analyzed_at is not None
        assert job.analysis.fit_score == 0.88
        assert job.analysis.required_skills == ["Python"]
        assert (
            job.analysis.raw_response["supporting_excerpts"] == offer_result().supporting_excerpts
        )
        assert job.analysis.raw_response["offer_risks"] == offer_result().offer_risks
        cached = session.query(LLMCache).filter_by(purpose="job_offer_analysis").one()
        assert json.loads(cached.response)["supporting_excerpts"] == extracted.supporting_excerpts
    from smartapply.pipeline.apply.cv_writer import CvWriterMixin

    _, restored = CvWriterMixin()._load_job_analysis(job_id)
    assert restored.fit_score == 0.88
    assert restored.supporting_excerpts == offer_result().supporting_excerpts
    assert restored.offer_risks == offer_result().offer_risks
    # Cache retention cannot erase the source of a completed extraction.
    with session_scope() as session:
        assert purge_expired_cache(
            session, ttl_days=15, now=datetime.now(timezone.utc) + timedelta(days=16)
        ) > 0
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert session.query(LLMCache).count() == 0
        source = job.analysis.fit_diagnostics["extraction_provenance"]
        assert source["analyzer_input"]["offer_body"] == BODY
        assert source["extraction_key"]
        assert job.analysis.fit_diagnostics["offer_facts"]["required_skills"] == ["Python"]
        reused, provenance = reusable_offer(session, job)
        assert reused is not None
        assert provenance == source


@pytest.mark.parametrize("change", ["none", "profile", "body", "model", "prompt", "schema"])
def test_failed_evaluation_checkpoint_obeys_exact_extraction_contract(
    isolated_db, monkeypatch, change
):
    from smartapply.database.analysis_checkpoints import AnalysisCheckpoint

    calls = []
    fail_fit = True

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "job_offer_analysis":
            return offer_result()
        if fail_fit:
            raise LLMError("Evaluation temporarily unavailable")
        return evaluation_from_user(kwargs["user"])

    instance = runner(SimpleNamespace(complete_json=complete, cheap_model="mock"))
    job_id = insert_job()
    with pytest.raises(LLMError):
        instance._analyze_one(job_id=job_id)
    fail_fit = False
    if change == "body":
        with session_scope() as session:
            session.get(Job, job_id).description += " Work on a new team."
    elif change == "model":
        instance.llm.cheap_model = "mock-another-model"
    elif change == "profile":
        instance.profile.skills.categories[0].skills.append("NEW_SKILL")
    elif change == "prompt":
        original = job_analysis.build_system_prompt()
        monkeypatch.setattr(job_analysis, "build_system_prompt", lambda: original + " Revised.")
    elif change == "schema":
        original = OfferAnalysis.model_json_schema()
        monkeypatch.setattr(
            OfferAnalysis, "model_json_schema", lambda: {**original, "version": "new"}
        )
    assert instance._analyze_one(job_id=job_id, allow_existing=False)
    expected_extractions = 1 if change in {"none", "profile"} else 2
    assert calls.count("job_offer_analysis") == expected_extractions
    assert calls.count("job_fit_assessment") == 2
    with session_scope() as session:
        assert session.get(AnalysisCheckpoint, job_id) is None
    assert instance.analyze_jobs([job_id]).already_analyzed == 1
    assert calls.count("job_offer_analysis") == expected_extractions


def test_cancel_after_valid_extraction_keeps_checkpoint_before_stopping(isolated_db):
    from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
    from smartapply.pipeline.errors import TaskCancelledError
    from smartapply.pipeline.snapshots import CancellableProvider

    stopped = False
    calls = []

    def complete(**kwargs):
        nonlocal stopped
        calls.append(kwargs["purpose"])
        stopped = True
        return offer_result()

    instance = runner(
        CancellableProvider(
            SimpleNamespace(cheap_model="mock", complete_json=complete),
            lambda: stopped,
        )
    )
    instance.stop_requested = lambda: stopped
    job_id = insert_job()
    with pytest.raises(TaskCancelledError):
        instance._analyze_one(job_id=job_id)
    assert calls == ["job_offer_analysis"]
    with session_scope() as session:
        assert session.get(AnalysisCheckpoint, job_id) is not None
        assert session.get(Job, job_id).analysis is None
