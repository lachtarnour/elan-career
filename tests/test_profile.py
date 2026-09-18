"""Tests for the candidate profile module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from smartapply.profile import (
    load_profile,
)


def test_loads_default_profile_with_fictional_data() -> None:
    profile = load_profile()
    assert profile.identity.full_name == "Camille Martin"
    assert profile.identity.email.endswith("@example.com")
    assert profile.identity.location == "Paris, France"
    assert len(profile.experiences) >= 3
    assert len(profile.projects) >= 5
    assert len(profile.education) == 2
    education_by_id = {degree.id: degree for degree in profile.education}
    assert education_by_id["edu_masters"].start_date == "09/2021"
    assert education_by_id["edu_masters"].end_date == "09/2023"
    assert education_by_id["edu_bachelors"].start_date == "09/2018"
    assert education_by_id["edu_bachelors"].end_date == "06/2021"


# -------------------- New schema features --------------------


def test_published_example_profile_is_valid() -> None:
    profile_dir = Path(__file__).resolve().parents[1] / "smartapply/profile/mock_profile"
    profile = load_profile(profile_dir)
    assert profile.identity.email.endswith("@example.com")
    assert profile.experiences
    assert profile.skills.allowed_skills


# Helper used by several tests above
def _copy_profile(tmp_path: Path) -> Path:
    from smartapply.config import get_settings

    src = get_settings().profile_dir
    for f in src.iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    return tmp_path


def test_profile_has_targeted_skill_profiles() -> None:
    profile = load_profile()
    assert {
        "mixed",
        "llm",
        "machine_learning",
        "reinforcement_learning",
        "data_analyst",
        "time_series",
        "computer_vision",
        "speech_audio",
        "medical_ai",
        "operations_research",
    }.issubset(profile.skills.profile_ids)
    data_analyst = profile.skills.profile_by_id("data_analyst")
    assert data_analyst is not None
    assert "SQL" in data_analyst.category_skills["data_analysis"]
    assert "Streamlit" in data_analyst.category_skills["data_analysis"]
    assert "Panel" not in data_analyst.category_skills["data_analysis"]
    assert "Time-series analysis" not in data_analyst.category_skills["data_analysis"]
    assert "Time-series analysis" in data_analyst.category_skills["stats_signal"]
    medical_ai = profile.skills.profile_by_id("medical_ai")
    assert medical_ai is not None
    assert "Facial affect estimation" in medical_ai.category_skills["computer_vision"]
    operations_research = profile.skills.profile_by_id("operations_research")
    assert operations_research is not None
    assert operations_research.category_skills == {
        "stats_signal": [
            "Mathematical optimization",
            "Linear Programming",
            "Mixed-Integer Programming",
            "Optimization",
            "Monte Carlo methods",
        ],
        "data_analysis": ["Python", "NumPy", "SciPy"],
    }


def test_matching_keywords_are_kept_separate_from_display_skills() -> None:
    profile = load_profile()
    assert "llm" in profile.skills.matching_keywords
    assert "genai" in profile.skills.matching_keywords["llm"]
    assert "machine learning" in profile.skills.matching_keywords["machine_learning"]
    assert "computer vision" in profile.skills.matching_keywords["computer_vision"]
    assert "data analysis" in profile.skills.matching_keywords["data_analyst"]
    assert "reinforcement learning" in profile.skills.matching_keywords["reinforcement_learning"]
    assert "operations research" in profile.skills.matching_keywords["operations_research"]
    assert "mixed-integer programming" in profile.skills.matching_keywords["operations_research"]
    assert "genai" not in profile.skills.allowed_skills


def test_new_skills_are_local_to_categories_and_operations_research_profile() -> None:
    from smartapply.cv.role_contracts import load_contracts

    profile = load_profile()
    by_category = {category.id: set(category.skills) for category in profile.skills.categories}
    assert "Mathematical optimization" in by_category["stats_signal"]
    assert "Linear Programming" in by_category["stats_signal"]
    assert "Mixed-Integer Programming" in by_category["stats_signal"]
    assert "PySpark" in by_category["data_infra"]
    assert "Clinical Trial Reporting" in by_category["data_analysis"]

    new_skills = {
        "Mathematical optimization",
        "Linear Programming",
        "Mixed-Integer Programming",
        "PySpark",
        "Clinical Trial Reporting",
    }
    core_skills = {skill for skills in profile.skills.core.values() for skill in skills}
    assert new_skills.isdisjoint(core_skills)

    contracts = load_contracts()
    baseline_skills = {
        skill for skills in contracts["_global_baseline"]["skills"].values() for skill in skills
    }
    assert new_skills.isdisjoint(baseline_skills)

    contract_skills = set()
    for family_id, contract in contracts.items():
        if family_id == "_global_baseline":
            continue
        for field in ("must_show", "fill_skills"):
            for skills in contract.get(field, {}).values():
                contract_skills.update(skills)
        contract_skills.update(contract.get("forbidden", []))
    assert new_skills.isdisjoint(contract_skills)


def test_bullet_ids_are_globally_unique() -> None:
    profile = load_profile()
    bullet_ids = [b.id for e in profile.experiences for b in e.bullets]
    assert len(set(bullet_ids)) == len(bullet_ids)


def test_duplicate_bullet_ids_rejected(tmp_path: Path, sample_profile_files: Path) -> None:
    assert sample_profile_files == tmp_path
    # Inject duplicate bullet id and reload
    exps_path = tmp_path / "experiences.json"
    exps = json.loads(exps_path.read_text())
    exps[0]["bullets"].append(exps[0]["bullets"][0])  # duplicate
    exps_path.write_text(json.dumps(exps))
    with pytest.raises(ValidationError):
        load_profile(tmp_path)


@pytest.fixture
def sample_profile_files(tmp_path: Path) -> Path:
    """Copy the fictional profile data into tmp_path so tests can mutate it safely."""
    from smartapply.config import get_settings

    src = get_settings().profile_dir
    for f in src.iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    return tmp_path
