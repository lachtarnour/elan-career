"""Tests for role-family classification and post-LLM skill composition.

The contract retains catalogue-backed skills selected by the LLM, including
skills outside a family's usual categories. Family rules still govern automatic
additions and supply a baseline when the LLM selection is sparse.
"""

from __future__ import annotations

import pytest

from smartapply.cv.role_contracts import apply_contract, load_contracts
from smartapply.cv.role_family import (
    KNOWN_ROLE_FAMILIES,
    classify,
    classify_title,
    cv_title_family_is_compatible,
    has_data_scientist_ia_signal,
)
from smartapply.llm import (
    AdaptedCV,
    JobAnalysis,
    SkillSelectionBlock,
)
from smartapply.profile import get_profile


def _analysis(
    *,
    role_type: str,
    main_tasks: list[str] | None = None,
    required: list[str] | None = None,
    keywords: list[str] | None = None,
    nice_to_have: list[str] | None = None,
    domain: str = "Tech",
) -> JobAnalysis:
    return JobAnalysis(
        role_type=role_type,
        seniority="mid",
        domain=domain,
        main_tasks=main_tasks or [],
        required_skills=required or [],
        nice_to_have=nice_to_have or [],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=keywords or [],
    )


def _cv(selected: dict[str, list[str]]) -> AdaptedCV:
    return AdaptedCV(
        cv_title="Adapted CV",
        professional_summary="Adapted summary.",
        selected_experiences=[],
        selected_project_ids=[],
        selected_skills=[
            SkillSelectionBlock(category_id=cid, skills=skills) for cid, skills in selected.items()
        ],
        skills_order=list(selected.keys()),
        warnings=[],
    )


def _apply(cv: AdaptedCV, analysis: JobAnalysis, title: str) -> tuple[AdaptedCV, str]:
    profile = get_profile()
    allowed_lower = {s.lower() for s in profile.skills.allowed_skills}
    supported_by_category = {
        category.id: list(category.skills) for category in profile.skills.categories
    }
    return apply_contract(
        cv,
        analysis=analysis,
        job_title=title,
        allowed_skills_lower=allowed_lower,
        supported_skills_by_category=supported_by_category,
    )


def _selected_map(cv: AdaptedCV) -> dict[str, list[str]]:
    return {block.category_id: list(block.skills) for block in cv.selected_skills}


# ---------------- classify() ----------------


def test_contract_file_covers_all_classifier_families():
    contracts = load_contracts()
    assert KNOWN_ROLE_FAMILIES.issubset(set(contracts))


def test_title_family_wins_over_noisy_offer_body():
    """Regression benchmark: explicit titles should not be rerouted by skills."""
    product_analyst = _analysis(
        role_type="Product Data Analyst",
        main_tasks=["Build dashboards and analyze customer journey"],
        required=["SQL", "Snowflake", "Power BI"],
        keywords=["Machine learning", "dashboards"],
    )
    assert (
        classify(
            product_analyst,
            title="Product Data Analyst F/H - Système, réseaux, données",
        )
        == "data_analyst"
    )

    backend = _analysis(
        role_type="Backend Software Engineer",
        main_tasks=[
            "Design backend features",
            "Collaborate with data science and product teams",
        ],
        required=["Node.js", "API development", "Software architecture"],
        keywords=["data engineering", "software architecture"],
    )
    assert classify(backend, title="Backend Software Engineer (H/F)") == "software_engineer"

    ds_llm = _analysis(
        role_type="Data Scientist",
        main_tasks=["Integrate NLP and generative AI into support projects"],
        required=["Machine Learning", "NLP", "Databricks"],
        keywords=["NLP", "Data pipelines"],
    )
    assert classify(ds_llm, title="Data scientist (H/F)") == "data_scientist"
    assert has_data_scientist_ia_signal(ds_llm, "Data scientist (H/F)")


def test_cv_title_classifier_uses_only_broad_professional_family():
    assert classify_title("AI Engineer - Applied Intelligence") == "ml_engineer"
    assert classify_title("Data Scientist - NLP & Multimodal AI") == "data_scientist"
    assert classify_title("Kubernetes Specialist") == "other"


def test_langgraph_offer_is_recognized_as_llm_without_overriding_explicit_role():
    analysis = _analysis(role_type="AI Engineer", required=["LangGraph"])
    assert classify(analysis, title="AI Engineer") == "llm_engineer"
    assert classify(analysis, title="LangGraph Developer") == "llm_engineer"
    assert classify(analysis, title="Data Scientist") == "data_scientist"


def test_specialist_offer_accepts_stable_ml_engineer_title():
    assert cv_title_family_is_compatible("llm_engineer", "ml_engineer")
    assert cv_title_family_is_compatible("computer_vision", "ml_engineer")
    assert not cv_title_family_is_compatible("data_engineer", "data_scientist")


def test_ambiguous_title_family_does_not_create_false_warning():
    assert cv_title_family_is_compatible("data_engineer", "other")
    assert cv_title_family_is_compatible("other", "data_scientist")


# ---------------- apply_contract() ----------------


def test_data_scientist_anchors_ml_baseline():
    """A DS offer that mentions no AI keyword still keeps PyTorch + scikit-learn."""
    cv = _cv({"data_analysis": ["Python", "SQL"]})
    analysis = _analysis(
        role_type="Data Scientist",
        main_tasks=["Build predictive models on customer data"],
    )
    adapted, family = _apply(cv, analysis, "Data Scientist")
    assert family == "data_scientist"
    selected = _selected_map(adapted)
    assert "PyTorch" in selected["ml_ai"]
    assert "scikit-learn" in selected["ml_ai"]
    assert "Python" in selected["data_analysis"]
    assert "NLP" not in selected.get("ml_ai", [])


def test_explicit_required_skill_lifts_forbidden_lock_for_automatic_additions():
    """A requested skill may be added despite a role-family forbidden rule."""
    analysis = _analysis(role_type="Data Analyst", required=["Git"])
    adapted, _family = _apply(_cv({}), analysis, "Data Analyst")
    selected = _selected_map(adapted)
    assert "Git" in selected.get("data_infra", [])


@pytest.mark.parametrize(
    ("title", "analysis", "category", "llm_skills", "expected_family"),
    [
        (
            "Data Analyst",
            _analysis(role_type="Data Analyst", nice_to_have=["Git", "CI/CD"]),
            "data_infra",
            ["Git", "CI/CD"],
            "data_analyst",
        ),
        (
            "PhD Researcher in Model Compression",
            _analysis(
                role_type="PhD Researcher",
                main_tasks=["Optimize compressed self-supervised models"],
            ),
            "stats_signal",
            ["Mathematical optimization"],
            "other",
        ),
        (
            "Research Scientist - Speech/Audio Machine Learning",
            _analysis(
                role_type="Research Scientist Speech/Audio Machine Learning",
                required=["Transformer-based audio LLMs"],
            ),
            "speech_audio",
            ["Pyannote", "openSMILE"],
            "llm_engineer",
        ),
        (
            "Développeur IA / LLM",
            _analysis(role_type="Développeur IA / LLM", required=["Computer Vision"]),
            "computer_vision",
            ["OpenCV", "Facial affect estimation"],
            "llm_engineer",
        ),
    ],
)
def test_contract_keeps_catalogue_skills_selected_by_llm_across_family_boundaries(
    title: str,
    analysis: JobAnalysis,
    category: str,
    llm_skills: list[str],
    expected_family: str,
) -> None:
    adapted, family = _apply(_cv({category: llm_skills}), analysis, title)

    assert family == expected_family
    displayed = _selected_map(adapted)[category]
    assert [skill for skill in displayed if skill in llm_skills] == llm_skills


def test_contract_does_not_keep_uncatalogued_llm_skill() -> None:
    adapted, _ = _apply(
        _cv({"data_analysis": ["Python", "Invented framework", "SQL"]}),
        _analysis(role_type="Data Analyst"),
        "Data Analyst",
    )

    selected = _selected_map(adapted)
    assert "Invented framework" not in {
        skill for skills in selected.values() for skill in skills
    }
    assert [skill for skill in selected["data_analysis"] if skill in {"Python", "SQL"}] == [
        "Python",
        "SQL",
    ]


def test_family_forbidden_still_gates_automatic_baseline_additions() -> None:
    adapted, family = _apply(
        _cv({}),
        _analysis(role_type="Data Analyst"),
        "Data Analyst",
    )

    assert family == "data_analyst"
    selected = _selected_map(adapted)
    # The global baseline proposes these skills, but the analyst contract
    # forbids adding them unless the offer explicitly asks for them.
    assert not {"Docker", "Git", "AWS"}.intersection(selected.get("data_infra", []))


def test_global_baseline_places_sql_in_its_catalogue_category():
    adapted, _ = _apply(
        _cv({}),
        _analysis(role_type="Reinforcement Learning Engineer"),
        "Reinforcement Learning Engineer",
    )
    selected = _selected_map(adapted)
    assert "SQL" in selected["data_analysis"]
    assert "SQL" not in selected.get("data_infra", [])


def _apply_order_contract(
    cv: AdaptedCV,
    *,
    supported: dict[str, list[str]],
    must_show: dict[str, list[str]] | None = None,
    category_order: list[str] | None = None,
) -> AdaptedCV:
    adapted, _ = apply_contract(
        cv,
        analysis=_analysis(role_type="Data Analyst"),
        job_title="Data Analyst",
        allowed_skills_lower={skill.lower() for skills in supported.values() for skill in skills},
        supported_skills_by_category=supported,
        contracts={
            "_global_baseline": {"enabled": False},
            "data_analyst": {
                "must_show": must_show or {},
                "category_order": category_order or [],
            },
        },
    )
    return adapted


def test_llm_order_precedes_additions_to_existing_and_new_categories() -> None:
    cv = _cv(
        {
            "data_analysis": ["SQL", "Python"],
            "ml_ai": ["scikit-learn", "PyTorch"],
        }
    )
    # The explicit LLM order, not the order of blocks or the role's priority,
    # determines which category the reader sees first.
    cv.skills_order = ["ml_ai", "data_analysis"]
    before = cv.model_dump()

    adapted = _apply_order_contract(
        cv,
        supported={
            "data_analysis": ["Python", "SQL", "pandas"],
            "ml_ai": ["PyTorch", "scikit-learn"],
            "data_infra": ["Docker", "Git"],
        },
        must_show={"data_analysis": ["pandas"], "data_infra": ["Docker", "Git"]},
        category_order=["data_infra", "data_analysis", "ml_ai"],
    )

    assert adapted.skills_order == ["ml_ai", "data_analysis", "data_infra"]
    assert [(block.category_id, block.skills) for block in adapted.selected_skills] == [
        ("ml_ai", ["scikit-learn", "PyTorch"]),
        ("data_analysis", ["SQL", "Python", "pandas"]),
        ("data_infra", ["Docker", "Git"]),
    ]
    assert cv.model_dump() == before


def test_code_added_duplicate_does_not_take_the_original_llm_category() -> None:
    cv = _cv(
        {
            "ml_ai": ["PyTorch", "NLP", "Transformers"],
            "computer_vision": ["PyTorch", "OpenCV"],
            "data_analysis": ["SQL", "Python"],
        }
    )
    # Simulate an offer guard that has already added a more specific category.
    # Its duplicate PyTorch must not displace the original LLM selection.
    cv.llm_selected_skills = [
        SkillSelectionBlock(category_id="ml_ai", skills=["PyTorch", "NLP", "Transformers"])
    ]

    adapted = _apply_order_contract(
        cv,
        supported={
            "ml_ai": ["PyTorch", "NLP", "Transformers"],
            "computer_vision": ["PyTorch", "OpenCV"],
            "data_analysis": ["SQL", "Python"],
        },
        category_order=["computer_vision", "data_analysis", "ml_ai"],
    )

    assert adapted.skills_order == ["ml_ai", "computer_vision", "data_analysis"]
    assert [(block.category_id, block.skills) for block in adapted.selected_skills] == [
        ("ml_ai", ["PyTorch", "NLP", "Transformers"]),
        ("computer_vision", ["OpenCV"]),
        ("data_analysis", ["SQL", "Python"]),
    ]
    # The order correction changes neither the selected set nor the additions.
    assert {skill for block in adapted.selected_skills for skill in block.skills} == {
        "PyTorch", "NLP", "Transformers", "OpenCV", "SQL", "Python"
    }


@pytest.mark.parametrize(
    ("llm_order", "expected_order"),
    [
        ([], ["data_analysis", "ml_ai", "data_infra"]),
        (["ml_ai"], ["ml_ai", "data_analysis", "data_infra"]),
        (
            ["unknown", "ml_ai", "ml_ai", "stats_signal"],
            ["ml_ai", "data_analysis", "data_infra"],
        ),
    ],
)
def test_missing_or_partial_llm_order_keeps_remaining_selected_blocks(
    llm_order: list[str], expected_order: list[str]
) -> None:
    selected = {
        "data_analysis": ["SQL", "Python"],
        "ml_ai": ["PyTorch", "scikit-learn"],
        "data_infra": ["Docker", "Git"],
    }
    cv = _cv({**selected, "stats_signal": ["Invented framework"]})
    cv.skills_order = llm_order

    adapted = _apply_order_contract(
        cv,
        supported={**selected, "stats_signal": ["Mathematical optimization"]},
        category_order=["data_infra", "ml_ai", "data_analysis"],
    )

    assert adapted.skills_order == expected_order
    assert [block.category_id for block in adapted.selected_skills] == expected_order
    assert _selected_map(adapted) == selected


def test_empty_llm_selection_does_not_promote_offer_guard_additions_to_llm_order() -> None:
    # None means provenance has not been captured; [] means the LLM really
    # selected no supported skill before the offer guard supplied these blocks.
    selected = {"data_analysis": ["SQL", "Python"], "data_infra": ["Docker", "Git"]}
    cv = _cv(selected)
    cv.llm_selected_skills = []

    adapted = _apply_order_contract(
        cv,
        supported=selected,
        category_order=["data_infra", "data_analysis"],
    )

    assert adapted.skills_order == ["data_infra", "data_analysis"]
    assert [block.category_id for block in adapted.selected_skills] == adapted.skills_order
    assert _selected_map(adapted) == selected
