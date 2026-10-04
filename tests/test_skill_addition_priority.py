"""Default priorities choose automatic complements without overruling the LLM."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from smartapply.cv.role_contracts import apply_contract, load_contracts
from smartapply.llm import AdaptedCV, JobAnalysis, SkillSelectionBlock


def _cv(selected: dict[str, list[str]]) -> AdaptedCV:
    return AdaptedCV(
        cv_title="Data Analyst",
        professional_summary="Profile summary.",
        selected_experiences=[],
        selected_project_ids=[],
        selected_skills=[
            SkillSelectionBlock(category_id=cid, skills=skills)
            for cid, skills in selected.items()
        ],
        skills_order=list(selected),
        warnings=[],
    )


def _contracts(
    priorities: dict[str, list[str]] | None,
    **role: Any,
) -> dict[str, dict[str, Any]]:
    contracts = {
        "_global_baseline": {"enabled": False},
        "data_analyst": {
            "must_show": {},
            "fill_skills": {},
            "forbidden": [],
            "min_total_skills": 0,
            **role,
        },
    }
    if priorities is not None:
        contracts["_skill_addition_priority"] = priorities
    return contracts


def _apply(
    cv: AdaptedCV,
    supported: dict[str, list[str]],
    contracts: dict[str, dict[str, Any]],
    *,
    allowed: set[str] | None = None,
) -> AdaptedCV:
    result, family = apply_contract(
        cv,
        analysis=JobAnalysis(
            role_type="Data Analyst",
            seniority="mid",
            domain="Tech",
            main_tasks=[],
            required_skills=[],
            nice_to_have=[],
            match_reasons=[],
            risks=[],
            cv_keywords_to_include=[],
        ),
        job_title="Data Analyst",
        allowed_skills_lower=(
            {skill.lower() for skills in supported.values() for skill in skills}
            if allowed is None else allowed
        ),
        supported_skills_by_category=supported,
        contracts=contracts,
    )
    assert family == "data_analyst"
    return result


def _selected(cv: AdaptedCV) -> dict[str, list[str]]:
    return {block.category_id: block.skills for block in cv.selected_skills}


def test_default_priorities_cover_nine_categories_and_keep_combined_skill() -> None:
    priorities = load_contracts()["_skill_addition_priority"]
    assert {cid: skills[0] for cid, skills in priorities.items()} == {
        "ml_ai": "PyTorch",
        "generative_agentic_ai": "LangChain",
        "knowledge_graphs": "RDF",
        "computer_vision": "Image processing",
        "speech_audio": "Speech processing",
        "data_analysis": "Python",
        "data_infra": "Git",
        "stats_signal": "Data imputation",
        "rl": "Q-learning",
    }
    skills = [skill for category in priorities.values() for skill in category]
    assert len(skills) == len({skill.casefold() for skill in skills}) == 64
    assert "Streamlit / Panel / Plotly" in priorities["data_analysis"]
    assert {"Streamlit", "Panel", "Plotly"}.isdisjoint(skills)


@pytest.mark.parametrize(
    ("category", "original", "profile_order", "expected"),
    [
        (
            "stats_signal", "Data imputation",
            ["ARIMA/SARIMA", "Stationarity tests", "Monte Carlo methods"],
            "Monte Carlo methods",
        ),
        (
            "computer_vision", "Segmentation",
            ["CNNs", "torchvision", "OpenCV", "Image processing"],
            "Image processing",
        ),
        ("rl", "MCTS", ["SARSA", "Policy gradients", "Q-learning"], "Q-learning"),
    ],
)
def test_singleton_uses_approved_priority_instead_of_profile_order(
    category: str, original: str, profile_order: list[str], expected: str,
) -> None:
    result = _apply(
        _cv({category: [original]}),
        {category: [original, *profile_order]},
        _contracts(load_contracts()["_skill_addition_priority"]),
    )
    assert _selected(result) == {category: [original, expected]}


def test_singleton_ranks_profile_candidates_together_with_contract_candidates() -> None:
    result = _apply(
        _cv({"stats_signal": ["Data imputation"]}),
        {"stats_signal": ["ARIMA/SARIMA", "Data imputation", "Monte Carlo methods"]},
        _contracts(
            load_contracts()["_skill_addition_priority"],
            fill_skills={"stats_signal": ["ARIMA/SARIMA"]},
        ),
    )
    assert _selected(result)["stats_signal"] == ["Data imputation", "Monte Carlo methods"]


def test_priority_does_not_bypass_catalogue_family_or_duplicate_rules() -> None:
    supported = {
        "stats_signal": [
            "Mathematical optimization", "Data imputation", "Monte Carlo methods",
            "ARIMA/SARIMA", "Stationarity tests",
        ],
        "ml_ai": ["PyTorch", "Git"],
    }
    allowed = {skill.lower() for skills in supported.values() for skill in skills}
    allowed.remove("data imputation")
    # A priority entry is not itself a source of permitted addition candidates.
    allowed.add("rank-only skill")
    result = _apply(
        _cv({
            "stats_signal": ["Mathematical optimization"],
            "ml_ai": ["ARIMA/SARIMA", "PyTorch"],
        }),
        supported,
        _contracts(
            {"stats_signal": [
                "Rank-only skill", "Data imputation", "Monte Carlo methods",
                "Git", "ARIMA/SARIMA", "Invented skill", "Stationarity tests",
            ]},
            fill_skills={"stats_signal": [
                "Git", "Invented skill", "ARIMA/SARIMA", "Stationarity tests",
            ]},
            forbidden=["Monte Carlo methods"],
        ),
        allowed=allowed,
    )
    assert _selected(result) == {
        "stats_signal": ["Mathematical optimization", "Stationarity tests"],
        "ml_ai": ["ARIMA/SARIMA", "PyTorch"],
    }


def test_minimum_total_ranks_only_existing_fill_pool_within_each_category() -> None:
    result = _apply(
        _cv({"data_analysis": ["SQL", "Python"]}),
        {
            "data_analysis": ["Python", "SQL"],
            "stats_signal": [
                "ARIMA/SARIMA", "Data imputation", "Monte Carlo methods",
                "Mathematical optimization",
            ],
            "ml_ai": ["PyTorch", "scikit-learn"],
        },
        _contracts(
            {
                "stats_signal": [
                    "Mathematical optimization", "Data imputation",
                    "Monte Carlo methods", "ARIMA/SARIMA",
                ],
                "ml_ai": ["scikit-learn", "PyTorch"],
            },
            min_total_skills=4,
            fill_skills={
                "stats_signal": ["ARIMA/SARIMA", "Monte Carlo methods", "Data imputation"],
                "ml_ai": ["PyTorch", "scikit-learn"],
            },
        ),
    )
    assert _selected(result) == {
        "data_analysis": ["SQL", "Python"],
        "stats_signal": ["Data imputation", "Monte Carlo methods"],
    }


def test_only_added_suffixes_are_ranked_and_reapplication_is_stable() -> None:
    cv = _cv({"ml_ai": ["NLP", "PyTorch"], "data_analysis": ["SQL", "Python"]})
    # The explicit LLM family order differs from the raw blocks and contract.
    cv.skills_order = ["data_analysis", "ml_ai"]
    supported = {
        "ml_ai": ["PyTorch", "NLP", "TensorFlow", "scikit-learn"],
        "data_analysis": ["Python", "SQL", "NumPy", "pandas"],
        "data_infra": ["Docker", "Git"],
    }
    contracts = _contracts(
        load_contracts()["_skill_addition_priority"],
        category_order=["data_infra", "ml_ai", "data_analysis"],
        must_show={
            "ml_ai": ["TensorFlow", "scikit-learn"],
            "data_analysis": ["NumPy", "pandas"],
            "data_infra": ["Docker", "Git"],
        },
    )
    cv_before = cv.model_dump()
    supported_before, contracts_before = deepcopy(supported), deepcopy(contracts)

    result = _apply(cv, supported, contracts)

    assert result.skills_order == ["data_analysis", "ml_ai", "data_infra"]
    assert _selected(result) == {
        "data_analysis": ["SQL", "Python", "pandas", "NumPy"],
        "ml_ai": ["NLP", "PyTorch", "scikit-learn", "TensorFlow"],
        "data_infra": ["Git", "Docker"],
    }
    assert _apply(result, supported, contracts).model_dump() == result.model_dump()
    assert cv.model_dump() == cv_before
    assert supported == supported_before
    assert contracts == contracts_before


@pytest.mark.parametrize("priorities", [None, {}, {"ml_ai": ["PyTorch"]}])
def test_absent_category_priority_preserves_existing_candidate_precedence(
    priorities: dict[str, list[str]] | None,
) -> None:
    result = _apply(
        _cv({"stats_signal": ["Data imputation"]}),
        {"stats_signal": ["Data imputation", "Monte Carlo methods", "ARIMA/SARIMA"]},
        _contracts(priorities, fill_skills={"stats_signal": ["ARIMA/SARIMA"]}),
    )
    assert _selected(result)["stats_signal"] == ["Data imputation", "ARIMA/SARIMA"]


def test_priority_matching_is_normalized_and_unranked_additions_keep_their_order() -> None:
    result = _apply(
        _cv({}),
        {"data_analysis": ["Python", "SQL", "NumPy", "pandas"]},
        _contracts(
            {"data_analysis": ["  pAnDaS  ", "  SQL  "]},
            must_show={"data_analysis": ["NumPy", "Python", "SQL", "pandas"]},
        ),
    )
    assert _selected(result)["data_analysis"] == ["pandas", "SQL", "NumPy", "Python"]
