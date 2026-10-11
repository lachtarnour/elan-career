"""Immediate evaluations freeze and verify the profile source chosen by the caller."""

from __future__ import annotations

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.llm import MockLLMProvider
from smartapply.llm.fit_context import candidate_facts, fingerprint
from smartapply.pipeline import Pipeline
from smartapply.pipeline.errors import StaleTaskResultError
from smartapply.profile import get_profile
from tests.fit_fixtures import evaluation_from_user
from tests.test_two_stage_analysis import extraction_offer_result, insert_job


def pipeline_with_profile_source(monkeypatch, *, explicit=False):
    current = [get_profile().model_copy(deep=True)]
    monkeypatch.setattr("smartapply.pipeline.pipeline.get_profile", lambda: current[0])
    monkeypatch.setattr("smartapply.pipeline.process.analysis.get_profile", lambda: current[0])
    supplied = current[0].model_copy(deep=True)
    supplied.skills.categories[0].skills.append("EXPLICIT_PROFILE_ONLY")
    calls = []

    def fit(**kwargs):
        calls.append(kwargs)
        return evaluation_from_user(kwargs["user"])

    llm = MockLLMProvider({"job_offer_extraction": extraction_offer_result(), "job_fit_assessment": fit})
    pipeline = Pipeline(
        profile=supplied if explicit else None, llm=llm
    )
    return pipeline, current, supplied, calls


@pytest.mark.parametrize("explicit", [False, True])
def test_queued_offer_uses_the_current_relevant_profile_source(isolated_db, monkeypatch, explicit):
    pipeline, current, supplied, calls = pipeline_with_profile_source(
        monkeypatch, explicit=explicit
    )
    current[0] = current[0].model_copy(deep=True)
    current[0].skills.categories[0].skills.append("GLOBAL_PROFILE_UPDATED")
    job_id = insert_job()
    report = pipeline.analyze_jobs([job_id])
    assert report.analyzed == 1 and report.errors == [] and len(calls) == 1
    expected = supplied if explicit else current[0]
    assert ("GLOBAL_PROFILE_UPDATED" in calls[0]["user"]) is not explicit
    assert ("EXPLICIT_PROFILE_ONLY" in calls[0]["user"]) is explicit
    with session_scope() as session:
        diagnostics = session.get(Job, job_id).analysis.fit_diagnostics
        assert diagnostics["context"]["candidate_fingerprint"] == fingerprint(
            candidate_facts(expected)
        )
        assert diagnostics["request_user"] == calls[0]["user"]
        skills = diagnostics["context"]["candidate_facts"]["declared_skills"][0]["skills"]
        assert ("GLOBAL_PROFILE_UPDATED" in skills) is not explicit
        assert ("EXPLICIT_PROFILE_ONLY" in skills) is explicit


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("change", ["facts", "visual"])
def test_change_during_call_checks_the_same_source_and_ignores_visual_changes(
    isolated_db, monkeypatch, explicit, change
):
    pipeline, current, supplied, calls = pipeline_with_profile_source(
        monkeypatch, explicit=explicit
    )
    original = pipeline.llm._registry["job_fit_assessment"]

    def fit(**kwargs):
        result = original(**kwargs)
        relevant = supplied if explicit else current[0]
        if change == "facts":
            relevant.experiences[0].bullets[0].text += " Changed during call"
        else:
            relevant.style_guide.tone = "New layout policy"
            relevant.projects[-1].selection_policy.tier = "disabled"
        return result

    pipeline.llm._registry["job_fit_assessment"] = fit
    job_id = insert_job()
    if change == "facts":
        with pytest.raises(StaleTaskResultError):
            pipeline._processor._analyze_one(job_id=job_id)
    else:
        assert pipeline._processor._analyze_one(job_id=job_id)
    assert len(calls) == 1  # No automatic evaluation retry.
    with session_scope() as session:
        saved = session.get(Job, job_id).analysis
        assert (saved is None) is (change == "facts")


def test_global_profile_change_during_explicit_evaluation_does_not_invalidate_it(
    isolated_db, monkeypatch
):
    pipeline, current, supplied, calls = pipeline_with_profile_source(monkeypatch, explicit=True)
    original = pipeline.llm._registry["job_fit_assessment"]

    def fit(**kwargs):
        result = original(**kwargs)
        current[0] = current[0].model_copy(deep=True)
        current[0].skills.categories[0].skills.append("UNRELATED_GLOBAL_CHANGE")
        return result

    pipeline.llm._registry["job_fit_assessment"] = fit
    job_id = insert_job()
    assert pipeline._processor._analyze_one(job_id=job_id) and len(calls) == 1
    with session_scope() as session:
        saved = session.get(Job, job_id).analysis
        assert saved.fit_diagnostics["context"]["candidate_fingerprint"] == fingerprint(
            candidate_facts(supplied)
        )
