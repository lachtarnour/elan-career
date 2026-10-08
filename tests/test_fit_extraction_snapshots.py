"""Reused offer facts keep their extraction fingerprint and provenance together."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.models import Job
from smartapply.llm import OfferAnalysis
from smartapply.llm.fit_context import fingerprint
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from smartapply.profile import get_profile
from tests.fit_fixtures import evaluation_from_user
from tests.test_evidence_fit import fixture, seeded_analysis, snapshot


def offer_block(user):
    return json.loads(
        user.split("=== IMMUTABLE OFFER ANALYSIS JSON ===\n", 1)[1].split("\n\n===", 1)[0]
    )


@pytest.mark.parametrize("missing_historical_field", [False, True])
def test_next_plan_reuses_reextracted_facts_with_their_own_provenance(
    isolated_db, missing_historical_field
):
    job_id = seeded_analysis(trace=False)
    if missing_historical_field:
        with session_scope() as session:
            analysis = session.get(Job, job_id).analysis
            raw = copy.deepcopy(analysis.raw_response)
            del raw["supporting_excerpts"]
            analysis.raw_response = raw
    before = snapshot(job_id)
    original = before["analysis"]["raw_response"]
    new_facts = fixture()[1].model_copy(update={"required_skills": ["Python", "Kubernetes"]})
    initial_plan = prepare_rescore_plan(job_ids=[job_id], include_extractions=True)
    assert initial_plan["entries"][0]["operation"] == "reextract_then_fit"
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return (
            new_facts if kwargs["schema"] is OfferAnalysis else evaluation_from_user(kwargs["user"])
        )

    provider = SimpleNamespace(complete_json=complete)
    assert execute_rescore_plan(initial_plan, provider, approved=True)[0]["status"] == "rescored"
    after_extraction = snapshot(job_id)
    diagnostics = after_extraction["analysis"]["fit_diagnostics"]
    # Saved score facts, hash and extraction source form one immutable snapshot.
    assert diagnostics["offer_facts"]["required_skills"] == ["Python", "Kubernetes"]
    assert diagnostics["offer_fingerprint"] == fingerprint(diagnostics["offer_facts"])
    assert diagnostics["extraction_provenance"]["operation"] == "reextract_then_fit"
    assert after_extraction["analysis"]["raw_response"]["required_skills"] == ["Python"]
    assert (
        "supporting_excerpts" in after_extraction["analysis"]["raw_response"]
    ) is not missing_historical_field
    next_plan = prepare_rescore_plan(job_ids=[job_id], force=True)
    entry = next_plan["entries"][0]
    assert entry["operation"] == "evaluate_fit"
    assert entry["offer_facts"] == diagnostics["offer_facts"]
    assert entry["extraction_provenance"] == diagnostics["extraction_provenance"]
    assert execute_rescore_plan(next_plan, provider, approved=True)[0]["status"] == "rescored"
    assert [call["purpose"] for call in calls] == [
        "job_offer_analysis",
        "job_fit_assessment",
        "job_fit_assessment",
    ]
    assert offer_block(calls[-1]["user"])["required_skills"] == ["Python", "Kubernetes"]
    after = snapshot(job_id)
    for section in ("job", "score", "application", "documents"):
        assert after[section] == before[section]
    for field in original.keys() - {"fit_score", "match_reasons", "risks"}:
        assert after["analysis"]["raw_response"][field] == original[field]
    assert after["analysis"]["fit_diagnostics"]["offer_facts"] == diagnostics["offer_facts"]


@pytest.mark.parametrize("problem", ["fingerprint", "source", "incomplete_facts"])
@pytest.mark.parametrize("historical_provenance", [False, True])
def test_invalid_recent_snapshot_never_authenticates_different_historical_facts(
    isolated_db, problem, historical_provenance
):
    job_id = seeded_analysis()
    with session_scope() as session:
        job = session.get(Job, job_id)
        provenance = copy.deepcopy(job.analysis.fit_diagnostics["extraction_provenance"])
        old_facts = {
            field: job.analysis.raw_response[field] for field in OfferAnalysis.model_fields
        }
        recent_facts = copy.deepcopy(old_facts)
        recent_facts["required_skills"].append("Kubernetes")
        recent_fingerprint = fingerprint(recent_facts)
        if problem == "fingerprint":
            recent_fingerprint = "wrong"
        elif problem == "source":
            provenance["analyzer_input"]["offer_body"] += " A new incompatible source."
        else:
            del recent_facts["supporting_excerpts"]
        job.analysis.fit_diagnostics = {
            "offer_facts": recent_facts,
            "offer_fingerprint": recent_fingerprint,
            "extraction_provenance": provenance,
        }
        if historical_provenance:
            # The fallback has its own extraction record, not the recent provenance.
            source = asdict(build_analyzer_input(job))
            session.add(
                AnalysisBatchRun(
                    id="historical-extraction",
                    state="completed",
                    model="mock",
                    profile_data=get_profile().model_dump(mode="json"),
                )
            )
            session.flush()
            session.add(
                AnalysisBatchItem(
                    run_id="historical-extraction",
                    job_id=job_id,
                    job_snapshot="historical",
                    title=job.title,
                    company=job.company,
                    analyzer_input=source,
                    offer_data=old_facts,
                    state="completed",
                )
            )
    plan = prepare_rescore_plan(job_ids=[job_id], force=True)
    entry = plan["entries"][0]
    assert entry["operation"] == ("evaluate_fit" if historical_provenance else "needs_extraction")
    if historical_provenance:
        assert entry["offer_facts"]["required_skills"] == ["Python"]
        assert entry["extraction_provenance"]["run_id"] == "historical-extraction"
        assert "offer_facts" not in entry["extraction_provenance"]
    else:
        assert entry["offer_facts"] is None and entry["extraction_provenance"] is None
