"""Fit decisions and frozen-source isolation; all provider responses are simulated."""

from __future__ import annotations

import copy
import itertools
import json
from types import SimpleNamespace

import pytest

from smartapply.config import get_llm_settings
from smartapply.llm.fit_context import (
    build_fit_context,
)
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.fit_response import read_fit_decisions
from smartapply.llm.fit_scoring import calculate_fit_decisions, evaluate_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.llm_policy import resolve_route
from smartapply.profile import get_profile
from tests.fit_fixtures import decision_response
from tests.test_batch_analysis import offer


@pytest.fixture(autouse=True)
def explicit_fit_routes(monkeypatch, tmp_path):
    from tests.scope_fixtures import set_test_routes

    set_test_routes(
        monkeypatch,
        tmp_path,
        {
            purpose: {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "reasoning": "medium",
                "max_output_tokens": tokens,
            }
            for purpose, tokens in (
                ("job_scope_screening", 6000),
                ("job_offer_extraction", 6000),
                ("job_fit_assessment", 12000),
            )
        },
    )


def inputs():
    context = build_fit_context(
        get_profile(), resolve_route(get_llm_settings(), "job_fit_assessment").model
    )
    from smartapply.llm.fit_context import LEGACY_POLICY

    context["policy"] = copy.deepcopy(LEGACY_POLICY)
    from smartapply.llm.schemas import JobFitDecisionEvaluation
    from smartapply.llm.structured import to_strict_schema

    context["schema"] = to_strict_schema(JobFitDecisionEvaluation)
    facts = offer()
    provenance = {
        "analyzer_input": {
            "offer_body": "SOURCE_FIGÉE_UNIQUE : Python ou Julia ; Kubernetes apprécié.",
            "title": "AI Engineer",
            "source": "test",
        }
    }
    return context, facts, provenance


@pytest.mark.parametrize(
    "states,score",
    [
        (("strong_fit", "strong_fit", "credible_fit"), 0.95),
        (("strong_fit", "strong_fit", "partial_core_fit"), 0.90),
        (("weak_support", "strong_fit", "strong_fit"), 0.74),
        (("strong_fit", "very_low_support", "strong_fit"), 0.55),
        (("strong_fit", "strong_fit", "strong_fit"), 1.0),
        (("very_low_support", "very_low_support", "very_low_support"), 0),
        (("strong_fit", "credible_fit", "credible_fit"), 0.84),
    ],
)
def test_single_decision_table_and_one_rounding(states, score):
    public, diagnostic = evaluate_fit(decision_response(states=states), *inputs()[:2])
    assert public.fit_score == score
    assert diagnostic["used_decisions"] == dict(
        zip(("missions", "essentials", "responsibilities"), states, strict=True)
    )
    assert diagnostic["policy_name"] == "evidence_fit"
    assert not {"applied_caps", "limiting_caps", "preference_conflict"} & diagnostic.keys()


@pytest.mark.parametrize("levels", list(itertools.product(range(5), repeat=3)))
def test_all_125_combinations_use_only_the_ordinal_weighted_index(levels):
    from decimal import ROUND_HALF_UP, Decimal

    states = (
        "very_low_support",
        "weak_support",
        "partial_core_fit",
        "credible_fit",
        "strong_fit",
    )
    expected = (
        sum(Decimal(weight * level) for weight, level in zip((35, 45, 20), levels, strict=True))
        / 400
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    score, diagnostic = calculate_fit_decisions(tuple(states[level] for level in levels))
    assert score == float(expected)
    assert diagnostic["final_score"] == str(expected)
    assert diagnostic["policy_name"] == "evidence_fit"


@pytest.mark.parametrize(
    "annotations",
    [
        {},
        {"dimensions": 7, "issues": "invalid", "neutral_offer_refs": None},
        {
            "dimensions": {
                "missions": {"findings": [{"candidate_refs": ["absent"], "support": "invalid"}]}
            }
        },
        {"issues": [{"code": "confirmed_constraint_conflict"}, {"code": "substantial_core_gap"}]},
        {"fit_score": 0.01, "offer_interpretations": [{"source_quote": "invented"}]},
        {"reliability": None},
        {"reliability": "invalid"},
    ],
)
def test_annotations_cannot_change_score_or_add_caps(annotations):
    context, facts, provenance = inputs()
    raw = {**decision_response(), **annotations}
    public, diagnostic = evaluate_fit(raw, context, facts, extraction_provenance=provenance)
    assert public.fit_score == 0.95
    assert diagnostic["diagnostic_checks"]["warnings"]
    assert diagnostic["evaluation"] == raw


@pytest.mark.parametrize("function", ["schema_observations", "inspect_evaluation", "_explanations"])
def test_future_inspection_exception_or_mutation_never_changes_decision(monkeypatch, function):
    context, facts, provenance = inputs()
    raw = decision_response()
    before = copy.deepcopy((raw, context, facts.model_dump(), provenance))

    def broken(*args):
        if hasattr(args[0], "raw"):
            args[0].raw["decision"]["missions"] = "very_low_support"
        elif isinstance(args[0], dict):
            args[0].clear()
        else:
            args[0].decision.missions = "very_low_support"
        raise RuntimeError("Future diagnostic check failed")

    monkeypatch.setattr(f"smartapply.llm.fit_scoring.{function}", broken)
    result, diagnostic = complete_fit(
        SimpleNamespace(name="openai", complete_json=lambda **kwargs: raw),
        context,
        facts,
        extraction_provenance=provenance,
    )
    assert result.fit_score == 0.95
    assert (raw, context, facts.model_dump(), provenance) == before
    assert any(
        "Future diagnostic check failed" in text
        for text in diagnostic["diagnostic_checks"]["warnings"]
    )


@pytest.mark.parametrize(
    "suffix",
    [
        ',"issues":[{"text":"cut',
        ',"reliability":',
        ',"offer_interpre',
        ',"iss',
        ',"trace',
        ',"\\u0069ss',
    ],
)
def test_annotations_truncated_after_three_decisions_remain_recoverable(suffix):
    content = json.dumps(decision_response())[:-1] + suffix
    parsed = read_fit_decisions(content)
    public, diagnostic = evaluate_fit(parsed, *inputs()[:2])
    assert public.fit_score == 0.95 and diagnostic["original_response"] == content


@pytest.mark.parametrize(
    "content",
    [
        '{"decision":{"missions":"strong_fit","essentials":"strong_fit"}}',
        '{"decision":{"missions":"strong_fit","essentials":"strong_fit","responsibilities":"str',
        json.dumps(decision_response()).replace('"credible_fit"', '"unsupported"'),
        json.dumps(decision_response()).replace('"strong_fit"', "4", 1),
        json.dumps(decision_response())[:-1] + ',"dec',
        json.dumps(decision_response())[:-2] + ',"respon',
        json.dumps(decision_response()).replace(
            '"missions": "strong_fit"', '"missions":"strong_fit","missions":"weak_support"'
        ),
    ],
)
def test_incomplete_or_contradictory_decisions_are_not_invented(content):
    with pytest.raises(LLMValidationError):
        read_fit_decisions(content)


def test_json_escapes_and_identical_duplicates_have_unambiguous_decisions():
    content = json.dumps(decision_response()).replace(
        '"missions": "strong_fit"', '"\\u006dissions":"strong_fit","missions":"strong_fit"'
    )
    assert read_fit_decisions(content).levels == (4, 4, 3)


def test_legacy_reader_keeps_three_valid_axes_with_unrelated_diagnostic():
    context, facts, _ = inputs()
    context["unrelated_diagnostic"] = {"invalid_diagnostic": True}
    public, diagnostics = evaluate_fit(decision_response(), context, facts)
    assert public.fit_score == 0.95
    assert diagnostics["used_decisions"] == decision_response()["decision"]
