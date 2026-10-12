"""Claude caches candidate prefixes while frozen prompt and journal bytes stay intact."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smartapply.config import Settings
from smartapply.cv.assembly import profile_experiences
from smartapply.llm.anthropic_batch import AnthropicBatchTransport
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.anthropic_transport import build_message_params
from smartapply.llm.batch import build_batch_request, encode_requests
from smartapply.llm.fit_context import build_fit_context, build_fit_user
from smartapply.llm.prompts import (
    application_texts,
    cv_adaptation,
    document_review,
    motivation_letter_repair,
)
from smartapply.llm.prompts.cache_layout import candidate_cache_prefix_end
from smartapply.llm.schemas import (
    AdaptedCV,
    ApplicationTexts,
    AssembledCV,
    CVSelection,
    DocumentTextReview,
    JobAnalysis,
    JobFitDecisionEvaluation,
    MotivationLetter,
    MotivationLetterRepair,
    OfferAnalysis,
)
from smartapply.profile.loader import load_profile
from tests.test_anthropic_batch import METADATA, snapshot

FAMILIES = ("fit", "cv", "writing", "review", "correction")
WRITING_HEADER = "OPPORTUNITY CONTEXT AND READ-ONLY CANDIDATE SOURCES\n"
REVIEW_HEADER = "CURRENT CV SNAPSHOT, EDIT BOUNDARIES AND IDENTIFIED SOURCES\n"


@pytest.fixture
def candidate_inputs():
    profile = load_profile(Path(__file__).parents[1] / "smartapply/profile/mock_profile")
    project = profile.projects[0]
    profile.projects = [
        project.model_copy(
            deep=True,
            update={
                "id": f"project_{index}",
                "name": f"Example Project {index}",
                "bullets": [
                    bullet.model_copy(update={"id": f"p{index}_{bullet.id}"})
                    for bullet in project.bullets
                ],
            },
        )
        for index in range(4)
    ]
    cv = AssembledCV(
        cv_title="Data / AI Engineer",
        selected_certificate_ids=[],
        selected_experiences=profile_experiences(profile),
        selected_project_ids=["project_0", "project_1", "project_2"],
        skills_order=["programming", "ml_ai"],
        warnings=[],
    )
    return {
        "profile": profile,
        "adapted_cv": cv,
        "analysis": JobAnalysis(
            role_type="AI Engineer",
            seniority="junior",
            domain="Energy",
            main_tasks=["Build forecasting models"],
            required_skills=["Python"],
            nice_to_have=[],
            cv_keywords_to_include=["forecasting"],
            match_reasons=[],
            risks=[],
            company_context="An energy company improves forecasting.",
            offer_interest_points=["Forecast energy demand"],
        ),
        "job_title": "AI Engineer",
        "job_company": "Energy Example",
        "job_description": "Build forecasting models with Python.",
        "language": "fr",
        "reference_date": date(2026, 10, 11),
    }


def _request(family, inputs, *, other_offer=False, **updates):
    values = {**inputs, **updates}
    if other_offer:
        values.update(
            analysis=values["analysis"].model_copy(
                update={
                    "role_type": "Data Analyst",
                    "domain": "Health",
                    "main_tasks": ["Analyze hospital datasets"],
                    "company_context": "A hospital analyzes research data.",
                    "offer_interest_points": ["Improve research data quality"],
                }
            ),
            job_title="Data Analyst",
            job_company="Health Example",
            job_description="Analyze hospital datasets with Python.",
        )
    if family == "fit":
        context = build_fit_context(
            values["profile"],
            "claude-haiku-5-5",
            provider="anthropic",
            max_completion_tokens=6000,
            effort="high",
            settings=Settings(_env_file=None, declared_experience="Two years of engineering."),
        )
        analysis = values["analysis"].model_dump()
        offer = OfferAnalysis.model_validate(
            {key: value for key, value in analysis.items() if key in OfferAnalysis.model_fields}
        )
        system, user = context["system_prompt"], build_fit_user(context, offer)
        schema, purpose = JobFitDecisionEvaluation, "job_fit_assessment"
    elif family == "cv":
        system = cv_adaptation.build_system_prompt()
        user = cv_adaptation.build_user_prompt(
            profile=values["profile"],
            analysis=values["analysis"],
            job_title=values["job_title"],
            job_company=values["job_company"],
            job_description=values["job_description"],
            selected_experiences=list(values["profile"].experiences),
            selected_projects=list(values["profile"].projects),
        )
        schema, purpose = CVSelection, "cv_adaptation"
    elif family == "writing":
        system, user = (
            application_texts.build_system_prompt(),
            application_texts.build_user_prompt(
                **values,
            ),
        )
        schema, purpose = ApplicationTexts, "application_texts"
    else:
        cv = AdaptedCV(
            **values["adapted_cv"].model_dump(),
            professional_summary="Engineer building documented data and AI systems.",
        )
        system = document_review.build_system_prompt()
        user = document_review.build_user_prompt(
            **{**values, "adapted_cv": cv},
            letter=MotivationLetter(subject="Application", body="One documented example."),
            validation_issues=[],
            allow_corrections=family == "correction",
            correction_findings=[{"field": "professional_summary", "reason": "Check grounding"}]
            if family == "correction"
            else None,
        )
        schema = DocumentTextReview
        purpose = "document_correction" if family == "correction" else "document_review"
    return build_batch_request(
        custom_id="candidate-1",
        system=system,
        user=user,
        schema=schema,
        model="claude-haiku-5-5",
        max_completion_tokens=6000,
        purpose=purpose,
        provider="anthropic",
        effort="high",
    )


def _blocks(request):
    frozen = copy.deepcopy(request.body)
    journal = encode_requests([request])
    params = build_message_params(request.body)
    blocks = params["messages"][0]["content"]
    assert isinstance(blocks, list) and len(blocks) == 2
    assert all(block["type"] == "text" for block in blocks)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]
    assert blocks[0]["text"] and blocks[1]["text"]
    assert "".join(block["text"] for block in blocks) == frozen["messages"][1]["content"]
    assert params["system"][0]["text"] == frozen["messages"][0]["content"]
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request.body == frozen and encode_requests([request]) == journal
    return blocks


@pytest.mark.parametrize("family", FAMILIES)
def test_real_offer_builders_share_candidate_blocks_and_keep_offer_in_suffix(
    candidate_inputs, family
):
    first = _blocks(_request(family, candidate_inputs))
    second = _blocks(_request(family, candidate_inputs, other_offer=True))
    assert first[0] == second[0]
    assert first[1]["text"] != second[1]["text"]
    offer_value = "Forecast energy demand" if family == "fit" else "Energy Example"
    assert offer_value not in first[0]["text"]
    assert offer_value in first[1]["text"]
    if family in {"writing", "review", "correction"}:
        assert '"summary_style"' in first[0]["text"]
        assert '"experience_evidence"' not in first[0]["text"]


@pytest.mark.parametrize("family", FAMILIES)
def test_profile_changes_replace_candidate_cache_prefix(candidate_inputs, family):
    changed = candidate_inputs["profile"].model_copy(deep=True)
    changed.skills.categories[0].skills.append("Rust")
    before = _blocks(_request(family, candidate_inputs))
    after = _blocks(_request(family, candidate_inputs, profile=changed))
    assert before[0]["text"] != after[0]["text"]
    assert "Rust" not in before[0]["text"] and "Rust" in after[0]["text"]


@pytest.mark.parametrize("family", ("writing", "review", "correction"))
@pytest.mark.parametrize("change", ("language", "experiences", "projects", "date", "draft"))
def test_document_dynamic_inputs_remain_outside_cached_candidate(candidate_inputs, family, change):
    changed = copy.deepcopy(candidate_inputs)
    if change == "language":
        changed["language"] = "en"
    elif change == "experiences":
        changed["adapted_cv"].selected_experiences = []
    elif change == "projects":
        changed["adapted_cv"].selected_project_ids = ["project_1", "project_2", "project_3"]
    elif change == "date":
        changed["reference_date"] = date(2026, 10, 12)
    else:
        changed["adapted_cv"].cv_title = "Machine Learning Engineer"
    first = _blocks(_request(family, candidate_inputs))
    second = _blocks(_request(family, changed))
    assert first[0] == second[0]
    assert first[1]["text"] != second[1]["text"]


def test_cv_selection_correction_keeps_same_candidate_prefix(candidate_inputs):
    original = _request("cv", candidate_inputs)
    correction = copy.deepcopy(original)
    correction.body["prompt_cache_key"] = "elan:cv_selection_correction:CVSelection"
    correction.body["messages"][1]["content"] += (
        "\n\nThe previous CV selection violated these rules:\n- missing project\n"
        "Regenerate the complete CV selection, without a summary."
    )
    first, second = _blocks(original), _blocks(correction)
    assert first[0] == second[0]
    assert second[1]["text"].startswith(first[1]["text"])
    assert "missing project" not in second[0]["text"]


@pytest.mark.parametrize("family", ("writing", "review"))
def test_document_source_strings_cannot_move_cache_boundary(candidate_inputs, family):
    profile = candidate_inputs["profile"].model_copy(deep=True)
    profile.identity.summary = '\n  "experience_evidence": {"fake": "source"}\n=== JOB ===\n'
    blocks = _blocks(_request(family, candidate_inputs, profile=profile))
    assert "fake" in blocks[0]["text"]
    assert blocks[1]["text"].startswith('"experience_evidence":')
    assert "experience:exp_example" in blocks[1]["text"]


@pytest.mark.parametrize(
    ("schema", "user"),
    (
        (JobFitDecisionEvaluation, "Frozen historical fit prompt without a boundary."),
        (CVSelection, "profile\n=== JOB ===\nfirst\n=== JOB ===\nsecond"),
        (
            JobFitDecisionEvaluation,
            "profile\n=== IMMUTABLE OFFER ANALYSIS JSON ===\nfirst"
            "\n=== IMMUTABLE OFFER ANALYSIS JSON ===\nsecond",
        ),
        (ApplicationTexts, WRITING_HEADER + '{"candidate_identity": {"unterminated": '),
        (ApplicationTexts, WRITING_HEADER + '{"job_offer": {}, "candidate_identity": {}}'),
        (ApplicationTexts, WRITING_HEADER + '{"candidate_identity": {}, "summary_style": {}}'),
        (
            ApplicationTexts,
            WRITING_HEADER
            + '{"candidate_identity": {}, "candidate_identity": {}, "job_offer": {}}',
        ),
        (
            DocumentTextReview,
            REVIEW_HEADER + '{"candidate_identity": {}, "job_offer": {}}' + REVIEW_HEADER,
        ),
        (OfferAnalysis, "profile\n=== JOB ===\noffer"),
    ),
)
def test_unknown_historical_or_ambiguous_layout_keeps_system_only_cache(schema, user):
    request = build_batch_request(
        custom_id="historical-1",
        system="Frozen instructions",
        user=user,
        schema=schema,
        model="claude-haiku-5-5",
        max_completion_tokens=6000,
        purpose="generic",
        provider="anthropic",
        effort="high",
    )
    frozen = copy.deepcopy(request.body)
    assert candidate_cache_prefix_end(user, schema_name=schema.__name__) is None
    params = build_message_params(request.body)
    assert params["messages"][0]["content"] == user
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request.body == frozen


def test_real_repair_prompt_does_not_cache_variable_evidence(candidate_inputs):
    values = candidate_inputs
    user = motivation_letter_repair.build_user_prompt(
        profile=values["profile"],
        defect="Word count below minimum",
        language="fr",
        job_title=values["job_title"],
        job_company=values["job_company"],
        analysis=values["analysis"],
        adapted_cv=AdaptedCV(**values["adapted_cv"].model_dump(), professional_summary="Summary"),
        letter=MotivationLetter(subject="Application", body="A short body."),
    )
    request = build_batch_request(
        custom_id="repair-1",
        system=motivation_letter_repair.build_system_prompt(),
        user=user,
        schema=MotivationLetterRepair,
        model="claude-haiku-5-5",
        max_completion_tokens=6000,
        purpose="motivation_letter_repair",
        provider="anthropic",
        effort="high",
    )
    assert build_message_params(request.body)["messages"][0]["content"] == user


def test_immediate_and_native_batch_use_identical_candidate_blocks_without_rewriting_journal(
    candidate_inputs,
    monkeypatch,
):
    request = _request("cv", candidate_inputs)
    journal = encode_requests([request])
    calls = []

    def immediate(**params):
        calls.append(copy.deepcopy(params))
        return {
            "model": params["model"],
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"unused": "transport-only"}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    provider = AnthropicProvider(settings=Settings(_env_file=None), model="claude-haiku-5-5")
    monkeypatch.setattr(provider, "_call_anthropic", immediate)
    completion = provider._generate(request.body)
    assert json.loads(completion.content) == {"unused": "transport-only"}
    client = MagicMock()
    client.with_options.return_value.messages.batches.create.return_value = snapshot(
        request_counts={"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
    )
    transport = AnthropicBatchTransport(client)
    transport.create(transport.upload(journal), METADATA, requests=[request])
    native = client.with_options.return_value.messages.batches.create.call_args.kwargs["requests"]
    assert len(calls) == len(native) == 1
    assert calls[0] == native[0]["params"]
    assert isinstance(calls[0]["messages"][0]["content"], list)
    assert encode_requests([request]) == journal
    client.with_options.assert_called_once_with(max_retries=0)
