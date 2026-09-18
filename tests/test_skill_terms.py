"""Skill names must stay consistent across display selection and reconstruction."""

from __future__ import annotations

import pytest

from smartapply.cv.adapter import CvAdapter
from smartapply.cv.role_contracts import _is_explicit_offer_skill
from smartapply.llm import AdaptedCV, JobAnalysis, MockLLMProvider
from smartapply.llm.prompts import application_draft
from smartapply.profile import get_profile
from smartapply.profile.schema import SkillCategory, Skills
from smartapply.ranking import MockEmbeddingsProvider
from smartapply.utils.skill_terms import skill_term_matches


def _profile_with_skill(skill: str):
    profile = get_profile().model_copy(deep=True)
    profile.skills = Skills(
        categories=[SkillCategory(id="ml_ai", name="Machine Learning & AI", skills=[skill])]
    )
    return profile


def _analysis(term: str) -> JobAnalysis:
    return JobAnalysis(
        role_type="ML Engineer",
        seniority="mid",
        domain="AI",
        main_tasks=[],
        required_skills=[term],
        nice_to_have=[],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=[],
    )


@pytest.mark.parametrize(
    ("skill", "term"),
    [
        ("GNN", "graph neural networks"),
        ("GNN", "Experience with Graph Neural Networks (GNNs)"),
        ("RNN", "recurrent neural network"),
        ("RNN", "Recurrent Neural Networks (RNNs)"),
        ("CNNs", "convolutional neural networks"),
        ("MCTS", "Monte Carlo Tree Search"),
        ("MCTS", "Experience with Monte Carlo Tree Search (MCTS)"),
        ("Speech-to-text", "speech to text"),
        ("Speech-to-text", "automatic speech recognition"),
        ("Speech-to-text", "ASR"),
        ("scikit-learn", "sklearn"),
        ("Tree-based models & boosting", "Tree-based & boosting models"),
        ("Data imputation", "Imputation techniques"),
        ("OVHcloud", "OVH"),
        ("Distributed training (PyTorch DDP)", "DDP"),
        ("Distributed training (PyTorch DDP)", "PyTorch DDP"),
        ("Distributed training (PyTorch DDP)", "distributed training"),
        ("ARIMA/SARIMA", "ARIMA"),
        ("PyTorch", "Experience with PyTorch"),
        ("Graph Theory", "théorie des graphes"),
        ("R", "R"),
    ],
)
def test_equivalent_offer_names_use_canonical_display_skill(skill: str, term: str) -> None:
    profile = _profile_with_skill(skill)
    analysis = _analysis(term)
    assert skill_term_matches(term, skill)
    assert _is_explicit_offer_skill(skill, {term})

    adapter = CvAdapter(profile, llm=MockLLMProvider(), embeddings=MockEmbeddingsProvider())
    cv = AdaptedCV(
        cv_title="ML Engineer",
        professional_summary="Summary",
        selected_experiences=[],
        selected_project_ids=[],
        selected_skills=[],
        skills_order=[],
        warnings=[],
    )
    selected = adapter._ensure_supported_offer_skills(cv, analysis)
    assert [(block.category_id, block.skills) for block in selected.selected_skills] == [
        ("ml_ai", [skill])
    ]


@pytest.mark.parametrize(
    ("skill", "term"),
    [
        ("Git", "digital signal processing"),
        ("Git", "GitHub Copilot"),
        ("FastAPI", "API"),
        ("Stationarity tests", "tests"),
        ("Model quantization", "model"),
        ("Model quantization", "ONNX"),
        ("GNN", "graph theory"),
        ("RNN", "LSTM"),
        ("MCTS", "Monte Carlo methods"),
        ("LangChain", "LangGraph"),
        ("Speech-to-text", "Whisper"),
        ("R", "Transformers"),
    ],
)
def test_related_terms_or_word_fragments_are_not_aliases(skill: str, term: str) -> None:
    assert not skill_term_matches(term, skill)
    assert not _is_explicit_offer_skill(skill, {term})
    assert CvAdapter._matched_allowed_skill_keys(term, {skill.lower(): skill}) == []


def test_summary_expanded_model_name_keeps_canonical_skill_visible() -> None:
    assert CvAdapter._summary_mentions_skill("Built graph neural networks for segmentation.", "GNN")
    assert not CvAdapter._summary_mentions_skill("Studied graph theory.", "GNN")


@pytest.mark.parametrize("allowed", [False, True])
def test_langgraph_follows_catalogue_without_obsolete_learning_note(allowed: bool) -> None:
    profile = _profile_with_skill("LangGraph" if allowed else "LangChain")
    analysis = _analysis("LangGraph")
    prompt = application_draft.build_user_prompt(
        profile=profile,
        analysis=analysis,
        job_title="Agentic AI Engineer",
        job_company="Acme",
        selected_experiences=[],
        selected_projects=[],
    )
    assert "do not mention LangGraph" not in prompt
    assert "Text-to-SQL" not in prompt
    assert "unsupported_offer_terms_not_to_claim" not in prompt
    catalogue = prompt.split("allowed_skills_by_category:\n", 1)[1].split(
        "core_skills_by_category:", 1
    )[0]
    assert ("LangGraph" in catalogue) == allowed
    assert CvAdapter._matched_allowed_skill_keys(
        "LangGraph", {skill.lower(): skill for skill in profile.skills.allowed_skills}
    ) == (["langgraph"] if allowed else [])
