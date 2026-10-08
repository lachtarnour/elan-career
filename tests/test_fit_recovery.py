"""Recover complete scoring inputs from truncated diagnostic tails, never guesses."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import Job, LLMCache
from smartapply.database.repository.llm_cache import cache_set
from smartapply.llm import OpenAIProvider
from smartapply.llm.batch import BatchResult
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.fit_response import read_fit_response
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.pipeline.batch_analysis.results import accept_cache, import_result
from smartapply.pipeline.process.analysis import _assemble_analysis, persist_job_analysis
from tests.test_batch_analysis import seed_jobs
from tests.test_evidence_fit import batch_fixture, fixture
from tests.test_fit_nonblocking import minimal, response


def payload(*, code="limited_question"):
    data = minimal()
    data["issues"] = [
        {
            "code": code,
            "axis": "responsibilities",
            "origin": "candidate_evidence",
            "offer_refs": ["main_tasks[0]"],
            "candidate_refs": [],
            "text": "Une explication interrompue à la fin",
        }
    ]
    data["neutral_offer_refs"] = []
    complete = json.dumps(data, ensure_ascii=False)
    partial = complete[: complete.index("interrompue") + 5]
    return complete, partial


@pytest.mark.parametrize(
    "code,expected",
    [
        ("limited_question", 0.95),
        ("material_core_question", 0.79),
        ("confirmed_constraint_conflict", 0.19),
    ],
)
def test_truncated_explanation_preserves_levels_and_completed_issue_codes(code, expected):
    context, facts, _ = fixture()
    complete, partial = payload(code=code)
    expected_public, _ = evaluate_fit(complete, context, facts)
    public, diagnostic = evaluate_fit(partial, context, facts)
    assert public.fit_score == expected_public.fit_score == expected
    assert diagnostic["used_levels"] == {"missions": 4, "essentials": 4, "responsibilities": 3}
    assert diagnostic["original_response"] == partial
    assert diagnostic["evaluation"]["issues"][0]["code"] == code
    assert "text" not in diagnostic["evaluation"]["issues"][0]
    assert any(
        "Truncated JSON" in warning for warning in diagnostic["diagnostic_checks"]["warnings"]
    )


@pytest.mark.parametrize("tail", ["3", "3.", "3e", "3e+", "-"])
def test_number_at_cut_is_not_treated_as_a_complete_level(tail):
    content = (
        '{"dimensions":{"missions":{"level":4},"essentials":{"level":4},"responsibilities":{"level":'
        + tail
    )
    with pytest.raises(LLMValidationError):
        read_fit_response(content)


@pytest.mark.parametrize(
    "content",
    [
        '{"dimensions":{"missions":{"level":4},"essentials":{"level":4}},"issues":[{"text":"cut',
        '{"dimensions":{"missions":{"level":4,"level":1},"essentials":{"level":4},"responsibilities":{"level":3}},"issues":[{"text":"cut',
        '{"dimensions":{"missions":{"level":4},"essentials":{"level":4},"responsibilities":{"level":3,"level":3',
        '{"dimensions":{"missions":{"level":4},"essentials":{"level":4},"responsibilities":{"level":3}},"dimensions":{"missions":{"level":0},"text":"cut',
        '{"text":"Fake dimensions and levels 4/4/3, \\"level\\":4',
    ],
)
def test_missing_or_ambiguous_levels_cannot_be_recovered(content):
    with pytest.raises(LLMValidationError):
        read_fit_response(content)


def test_truncated_duplicate_keys_keep_all_completed_codes_and_values():
    content = (
        json.dumps(minimal())[:-1]
        + ',"issues":[{"code":"material_core_question","code":"unknown","text":"cut'
    )
    context, facts, _ = fixture()
    public, diagnostic = evaluate_fit(content, context, facts)
    assert public.fit_score == 0.79 and diagnostic["applied_caps"] == {
        "material_core_question": "0.79"
    }
    assert diagnostic["original_response"] == content
    # An unfinished repeated numeric value must remain ambiguous.
    content = json.dumps(minimal())[:-1] + ',"dimensions":'
    with pytest.raises(LLMValidationError):
        read_fit_response(content)


def test_incomplete_code_does_not_invent_a_cap():
    content = json.dumps(minimal())[:-1] + ',"issues":[{"code":"material_core_question'
    context, facts, _ = fixture()
    public, diagnostic = evaluate_fit(content, context, facts)
    assert public.fit_score == 0.95 and diagnostic["applied_caps"] == {}


@pytest.mark.parametrize("suffix", ["\\", "\\u", "\\u12", '\\"', "\\\\"])
def test_unfinished_annotation_strings_and_escapes_keep_completed_levels(suffix):
    content = json.dumps(minimal())[:-1] + ',"issues":[{"text":"cut' + suffix
    assert read_fit_response(content).levels == (4, 4, 3)


@pytest.mark.parametrize("suffix", ["\\q", "\\u12z", "\n", '" unexpected'])
def test_syntax_errors_before_eof_are_not_repaired(suffix):
    content = json.dumps(minimal())[:-1] + ',"issues":[{"text":"cut' + suffix
    with pytest.raises(LLMValidationError, match="unreadable JSON"):
        read_fit_response(content)


@pytest.mark.parametrize("path", ["immediate", "batch", "cache"])
@pytest.mark.parametrize(
    "code,expected", [("limited_question", 0.95), ("material_core_question", 0.79)]
)
def test_recovered_score_is_saved_and_reused_without_paid_correction(
    isolated_db, monkeypatch, path, code, expected
):
    job_id = seed_jobs()[0]
    content = payload(code=code)[1]
    if path == "immediate":
        context, facts, _ = fixture()
        provider = OpenAIProvider()
        calls = []

        def api(**kwargs):
            calls.append(kwargs)
            return response(content, finish="length")

        monkeypatch.setattr(provider, "_call_openai", api)
        public, diagnostic = complete_fit(provider, context, facts)
        cached, cached_diagnostic = complete_fit(provider, context, facts)
        assert public == cached and len(calls) == 1
        assert any(
            "Truncated JSON" in warning
            for warning in cached_diagnostic["diagnostic_checks"]["warnings"]
        )
        with session_scope() as session:
            persist_job_analysis(
                session,
                session.get(Job, job_id),
                _assemble_analysis(facts, public),
                context["model"],
                fit_diagnostics=diagnostic,
            )
    else:
        with session_scope() as session:
            run, item, request, _, _ = batch_fixture(session, job_id)
            if path == "batch":
                import_result(
                    session,
                    run,
                    "evaluation",
                    {"body": request.body, "cache_key": request.cache_key, "item_ids": [item.id]},
                    BatchResult(
                        custom_id=request.custom_id, content=content, error_code="incomplete_output"
                    ),
                )
            else:
                cache_set(
                    session,
                    cache_key=request.cache_key,
                    model=run.model,
                    response=content,
                    prompt_tokens=1,
                    completion_tokens=1,
                )
                assert accept_cache(session, item, run, "evaluation", request)
            assert item.state == "completed", item.error
            assert (
                session.scalar(
                    select(LLMCache).where(LLMCache.cache_key == request.cache_key)
                ).response
                == content
            )
    with session_scope() as session:
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == expected and saved.raw_response["fit_score"] == expected
        assert saved.fit_diagnostics["original_response"] == content
        assert saved.fit_diagnostics["evaluation"]["issues"][0]["code"] == code
        assert any(
            "Truncated JSON" in warning
            for warning in saved.fit_diagnostics["diagnostic_checks"]["warnings"]
        )


@pytest.mark.parametrize(
    "suffix",
    [
        ',"dimension',
        ',"dimensions":{"missions":{"level":4,"lev',
    ],
)
def test_unfinished_scoring_keys_cannot_hide_potential_duplicates(suffix):
    content = json.dumps(minimal())[:-1] + suffix
    with pytest.raises(LLMValidationError):
        read_fit_response(content)


def test_number_terminated_by_whitespace_is_fully_read():
    content = '{"dimensions":{"missions":{"level":4},"essentials":{"level":4},"responsibilities":{"level":3 '
    assert read_fit_response(content).levels == (4, 4, 3)
