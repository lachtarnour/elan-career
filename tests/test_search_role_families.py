"""Tests for bilingual search-role ownership and expansion."""

from __future__ import annotations

import re

import pytest

from smartapply.pipeline.ingest.queries import (
    build_source_queries,
    build_source_query_plan,
)
from smartapply.pipeline.ingest.role_families import (
    ROLE_FAMILIES,
    ROLE_FAMILY_SETS,
    SEARCH_TERM_REPLACEMENTS,
    default_search_query,
    expand_role_terms,
    matching_role_families,
    normalize_role_title,
)


def _family_keys(*terms: str) -> set[str]:
    return {family.key for family in matching_role_families(terms)}


def test_canonical_families_keep_shared_alias_associations() -> None:
    aliases = [alias for family in ROLE_FAMILIES for alias in ROLE_FAMILY_SETS[family.key]]

    assert len(aliases) == 114
    assert len(set(aliases)) == 105


def test_every_configured_profile_role_has_its_own_family() -> None:
    expected = {
        "Data Scientist": "data_scientist",
        "Machine Learning Engineer": "machine_learning_engineer",
        "AI Engineer": "ai_engineer",
        "Research Engineer": "research_engineer",
        "Applied Scientist": "applied_scientist",
        "Computer Vision Engineer": "computer_vision_engineer",
        "Speech AI Engineer": "speech_audio_ai_engineer",
        "NLP Engineer": "nlp_engineer",
        "Data & AI Consultant": "data_ai_consultant",
        "Data Analyst": "data_analyst",
        "Analytics Engineer": "analytics_engineer",
    }

    for title, family_key in expected.items():
        assert family_key in _family_keys(title)


def test_ml_engineer_activates_the_whole_bilingual_family() -> None:
    expanded = expand_role_terms(["ML Engineer"])

    assert _family_keys("Ingenieur Machine Learning") == {"machine_learning_engineer"}
    assert expanded[0] == "ML Engineer"
    assert "Machine Learning Engineer" in expanded
    assert "Ingénieur Machine Learning" in expanded
    assert "Développeur Machine Learning" in expanded
    assert {normalize_role_title(title) for title in expanded}.issuperset(
        ROLE_FAMILY_SETS["machine_learning_engineer"]
    )


def test_shared_title_activates_both_families_but_is_searched_once() -> None:
    assert _family_keys("AI/ML Engineer") == {
        "ai_engineer",
        "machine_learning_engineer",
    }

    expanded = expand_role_terms(["AI/ML Engineer"])

    assert "Machine Learning Engineer" in expanded
    assert "AI Engineer" in expanded
    assert "Ingénieur Machine Learning" in expanded
    assert expanded.count("AI/ML Engineer") == 1


def test_specialized_vision_audio_and_consulting_synonyms_are_covered() -> None:
    assert _family_keys("Ingénieur IA Vision") == {"computer_vision_engineer"}
    assert _family_keys("Ingénieur reconnaissance vocale") == {"speech_audio_ai_engineer"}
    assert _family_keys("Consultant Data Science") == {"data_ai_consultant"}


def test_selected_modern_ai_titles_use_one_precise_alias_each() -> None:
    ai_engineer_titles = (
        "Applied AI Engineer",
        "Generative AI Engineer",
        "Agentic AI Engineer",
        "Multimodal AI Engineer",
        "Medical AI Engineer",
        "Healthcare Data Scientist",
        "AI Developer",
    )

    for title in ai_engineer_titles:
        assert _family_keys(title) == {"ai_engineer"}
    assert _family_keys("AI Scientist") == {"applied_scientist"}


def test_cross_specialty_titles_keep_all_families_but_one_search_term() -> None:
    assert _family_keys("Machine Learning Research Engineer") == {
        "machine_learning_engineer",
        "research_engineer",
    }
    assert _family_keys("Machine Learning Scientist") == {
        "applied_scientist",
        "data_scientist",
    }
    assert _family_keys("Computer Vision Research Engineer") == {
        "computer_vision_engineer",
        "research_engineer",
    }
    assert _family_keys("Speech Research Engineer") == {
        "research_engineer",
        "speech_audio_ai_engineer",
    }
    assert _family_keys("Conversational AI Engineer") == {
        "nlp_engineer",
        "speech_audio_ai_engineer",
    }

    expanded = expand_role_terms(["Computer Vision Engineer", "Research Engineer"])
    assert expanded.count("Computer Vision Research Engineer") == 1


def test_multiple_families_alternate_aliases_after_user_terms() -> None:
    expanded = expand_role_terms(["Data Scientist", "NLP Engineer"])

    assert expanded[:2] == ["Data Scientist", "NLP Engineer"]
    assert expanded[2:8] == [
        "Scientifique des données",
        "Ingénieur NLP",
        "Machine Learning Scientist",
        "Natural Language Processing Engineer",
        "Ingénieur Data Science",
        "Ingénieur traitement automatique du langage",
    ]


def test_source_query_plan_keeps_explicit_terms_ahead_of_alias_fallbacks() -> None:
    plan = build_source_query_plan(
        "francetravail",
        "ML Engineer OR Data Analyst",
    )

    assert plan.primary == ("ML Engineer", "Data Analyst")
    assert plan.fallbacks[:4] == (
        "Machine Learning Engineer",
        "Analyste Data",
        "Ingénieur Machine Learning",
        "Analyste de données",
    )


def test_source_query_shape_respects_provider_capabilities() -> None:
    query = "ML Engineer OR Data Analyst"
    serpapi = build_source_queries("serpapi", query)
    linkedin = build_source_queries("linkedin", query)
    france_travail = build_source_queries("francetravail", query)
    wttj = build_source_queries("welcometothejungle", query)

    assert len(serpapi) == 1
    assert "Machine Learning Engineer" in serpapi[0]
    assert "Analyste de données" in serpapi[0]
    assert " OR " in serpapi[0]
    assert linkedin == france_travail
    assert "Machine Learning Engineer" in linkedin
    assert "Analyste de données" in linkedin
    assert wttj == [query]


def test_source_query_plan_adds_doctoral_lanes_without_cdi_suffix() -> None:
    plan = build_source_query_plan(
        "francetravail",
        "Data Scientist CDI",
        include_research_cdd=True,
    )

    assert plan.primary[:4] == (
        "Data Scientist",
        "Data Scientist thèse",
        "Data Scientist doctorat",
        "Data Scientist PhD",
    )
    assert all(not query.endswith(" CDI") for query in plan.all_queries)


def test_serpapi_query_plan_includes_doctoral_terms_without_contract_suffix() -> None:
    queries = build_source_queries(
        "serpapi",
        "Data Scientist CDI",
        include_research_cdd=True,
    )

    assert len(queries) == 1
    assert "Data Scientist thèse" in queries[0]
    assert "Data Scientist doctorat" in queries[0]
    assert "Data Scientist PhD" in queries[0]
    assert " CDI" not in queries[0]


def test_unknown_custom_title_is_preserved_without_false_family_match() -> None:
    assert _family_keys("Engineering Manager") == set()
    assert expand_role_terms(["Engineering Manager"]) == ["Engineering Manager"]


def test_bi_business_product_and_linguist_titles_do_not_activate_a_family() -> None:
    excluded = (
        "Business Intelligence Engineer",
        "Développeur BI",
        "Ingénieur BI",
        "Analyste BI",
        "Product Data Analyst",
        "Product Analyst",
        "Linguiste informatique",
        "Business Consultant",
        "Vision Engineer",
        "Audio Engineer",
        "Sound Engineer",
    )

    for title in excluded:
        assert _family_keys(title) == set()


def test_default_search_activates_every_family_using_precise_titles() -> None:
    terms = default_search_query().split(" OR ")

    assert terms == [
        "AI Engineer",
        "Machine Learning Engineer",
        "Data Scientist",
        "AI Research Engineer",
        "Applied ML Scientist",
        "NLP Engineer",
        "Computer Vision Engineer",
        "Speech AI Engineer",
        "Data Analyst",
        "Analytics Engineer",
        "Technical AI Consultant",
    ]
    assert _family_keys(*terms) == {family.key for family in ROLE_FAMILIES}
    assert not {normalize_role_title(term) for term in terms} & SEARCH_TERM_REPLACEMENTS.keys()


@pytest.mark.parametrize("source", ["linkedin", "francetravail", "serpapi"])
@pytest.mark.parametrize("include_research_cdd", [False, True])
def test_legacy_profile_queries_never_reintroduce_retired_terms(
    source: str, include_research_cdd: bool,
) -> None:
    legacy_terms = [alias for family in ROLE_FAMILIES for alias in family.legacy_aliases]
    query = " OR ".join(f"{term} CDI" for term in legacy_terms)
    plan = build_source_query_plan(source, query, include_research_cdd=include_research_cdd)
    concrete = [term for query in plan.all_queries for term in query.split(" OR ")]
    base_terms = {
        normalize_role_title(re.sub(r"\s+(?:CDI|thèse|doctorat|PhD)$", "", term))
        for term in concrete
    }

    assert not base_terms & SEARCH_TERM_REPLACEMENTS.keys()
    assert "ai research engineer" in base_terms
    assert "applied ml scientist" in base_terms
    assert "technical ai consultant" in base_terms
    assert "consultant technique ia" in base_terms
    assert "speech ml engineer" in base_terms
    assert len(concrete) == len(set(concrete))


def test_replaced_user_terms_stay_in_primary_tier_and_are_deduplicated() -> None:
    plan = build_source_query_plan(
        "linkedin",
        "Research Engineer OR AI Research Engineer OR Applied Scientist",
        include_research_cdd=True,
    )

    assert plan.primary[:4] == (
        "AI Research Engineer", "Machine Learning Research Engineer",
        "Applied ML Scientist", "AI Research Scientist",
    )
    assert len(plan.primary) == 16
    assert "Applied ML Scientist PhD" in plan.primary
    assert len(plan.all_queries) == len(set(plan.all_queries))


def test_precision_changes_preserve_ai_engineering_healthcare_and_consulting() -> None:
    expanded = expand_role_terms(["IA Engineer", "Data Consultant", "Consultant Data"])

    assert expanded[0] == "IA Engineer"
    assert "AI Engineer" in expanded
    assert "Healthcare Data Scientist" in expanded
    assert "Applied AI Consultant" in expanded
    assert "Consultant Data Science" in expanded
    assert "Data Consultant" not in expanded
    assert "Consultant Data" not in expanded
