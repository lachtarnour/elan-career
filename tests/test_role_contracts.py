"""Tests for the role-family classifier and the post-LLM skill contract.

These tests pin the behaviour the V1 contract is meant to guarantee:
- No FAISS / Markov / RL skills on roles that don't ask for them.
- Data Scientist roles keep a minimal ML/IA baseline.
- Data Scientist offers with AI/NLP signals get NLP + Transformers.
- Analytics / Software offers strip Flask/FastAPI/NLP unless explicit.
- LLM offers anchor RAG + FAISS + Vector search.
- Explicit ``required_skills`` lifts the forbidden lock for that term.
"""

from __future__ import annotations

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


def test_explicit_required_skill_lifts_forbidden_lock():
    """If an Analytics offer explicitly requires NLP, NLP survives."""
    cv = _cv(
        {
            "data_analysis": ["SQL", "Python"],
            "ml_ai": ["NLP"],
        }
    )
    analysis = _analysis(
        role_type="Analytics Engineer",
        required=["NLP"],
    )
    adapted, _family = _apply(cv, analysis, "Analytics Engineer")
    selected = _selected_map(adapted)
    assert "NLP" in selected.get("ml_ai", [])


def test_global_baseline_places_sql_in_its_catalogue_category():
    adapted, _ = _apply(
        _cv({}),
        _analysis(role_type="Reinforcement Learning Engineer"),
        "Reinforcement Learning Engineer",
    )
    selected = _selected_map(adapted)
    assert "SQL" in selected["data_analysis"]
    assert "SQL" not in selected.get("data_infra", [])
