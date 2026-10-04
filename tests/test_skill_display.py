"""Tests for presentation-only compaction of secondary skill categories."""

from __future__ import annotations

from smartapply.cv.skill_display import compact_sparse_secondary_categories
from smartapply.llm import AdaptedCV, SkillSelectionBlock

_CONTRACTS = {
    "data_engineer": {
        "allowed_categories": ["data_analysis", "data_infra"],
    }
}


def _cv(skills: dict[str, list[str]]) -> AdaptedCV:
    return AdaptedCV(
        cv_title="Data Engineer",
        professional_summary="Data pipelines and applied AI systems.",
        selected_experiences=[],
        selected_project_ids=[],
        selected_skills=[
            SkillSelectionBlock(category_id=category_id, skills=items)
            for category_id, items in skills.items()
        ],
        skills_order=list(skills),
        warnings=[],
    )


def _selected(cv: AdaptedCV) -> dict[str, list[str]]:
    return {block.category_id: list(block.skills) for block in cv.selected_skills}


def test_sparse_secondary_category_merges_into_closest_existing_category() -> None:
    original = _cv(
        {
            "data_infra": ["Git", "Docker", "Spark"],
            "data_analysis": ["Python", "SQL", "Pandas"],
            "generative_agentic_ai": ["LangChain", "RAG"],
        }
    )

    compacted, merges = compact_sparse_secondary_categories(
        original,
        primary_family="data_engineer",
        contracts=_CONTRACTS,
    )

    assert _selected(compacted) == {
        "data_infra": ["Git", "Docker", "Spark", "LangChain", "RAG"],
        "data_analysis": ["Python", "SQL", "Pandas"],
    }
    assert [(merge.source_category, merge.target_category) for merge in merges] == [
        ("generative_agentic_ai", "data_infra")
    ]
    assert "generative_agentic_ai" in _selected(original)


def test_secondary_category_with_four_skills_stays_standalone() -> None:
    original = _cv(
        {
            "data_infra": ["Git", "Docker"],
            "generative_agentic_ai": ["LangChain", "RAG", "Vector search", "FAISS"],
        }
    )

    compacted, merges = compact_sparse_secondary_categories(
        original,
        primary_family="data_engineer",
        contracts=_CONTRACTS,
    )

    assert compacted == original
    assert merges == ()


def test_sparse_primary_category_is_not_merged() -> None:
    original = _cv(
        {
            "data_infra": ["Git", "Docker"],
            "data_analysis": ["Python", "SQL"],
        }
    )

    compacted, merges = compact_sparse_secondary_categories(
        original,
        primary_family="data_engineer",
        contracts=_CONTRACTS,
    )

    assert compacted == original
    assert merges == ()


def test_compaction_can_be_disabled_for_instant_rollback() -> None:
    original = _cv(
        {
            "data_infra": ["Git", "Docker"],
            "generative_agentic_ai": ["LangChain", "RAG"],
        }
    )

    compacted, merges = compact_sparse_secondary_categories(
        original,
        primary_family="data_engineer",
        enabled=False,
        contracts=_CONTRACTS,
    )

    assert compacted == original
    assert merges == ()


def test_offer_anchored_category_is_not_compacted() -> None:
    original = _cv(
        {
            "data_infra": ["Git", "Docker"],
            "generative_agentic_ai": ["LangChain", "RAG"],
        }
    )

    compacted, merges = compact_sparse_secondary_categories(
        original,
        primary_family="data_engineer",
        protected_categories={"generative_agentic_ai"},
        contracts=_CONTRACTS,
    )

    assert compacted == original
    assert merges == ()


def test_llm_categories_keep_their_order_while_code_categories_can_be_merged() -> None:
    original = _cv({
        "data_infra": ["Git", "Docker"],
        "speech_audio": ["Pyannote", "openSMILE"],
        "data_analysis": ["Python", "SQL"],
        "generative_agentic_ai": ["LangChain", "RAG"],
    })
    original.skills_order = ["speech_audio", "data_infra", "data_analysis", "generative_agentic_ai"]
    original.llm_selected_skills = [
        SkillSelectionBlock(category_id="speech_audio", skills=["Pyannote", "openSMILE"]),
        SkillSelectionBlock(category_id="data_infra", skills=["Git"]),
        SkillSelectionBlock(category_id="data_analysis", skills=["Python", "SQL"]),
    ]
    before = original.model_dump()

    compacted, merges = compact_sparse_secondary_categories(
        original, primary_family="data_engineer", contracts=_CONTRACTS,
    )

    assert compacted.skills_order == ["speech_audio", "data_infra", "data_analysis"]
    assert [block.category_id for block in compacted.selected_skills] == compacted.skills_order
    assert _selected(compacted) == {
        "speech_audio": ["Pyannote", "openSMILE"],
        "data_infra": ["Git", "Docker", "LangChain", "RAG"],
        "data_analysis": ["Python", "SQL"],
    }
    assert [(merge.source_category, merge.target_category) for merge in merges] == [
        ("generative_agentic_ai", "data_infra"),
    ]
    assert original.model_dump() == before
