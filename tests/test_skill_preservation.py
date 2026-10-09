"""Known LLM skills survive enrichment, regrouping and both document formats."""

from __future__ import annotations

import re
from html import unescape

import pytest
from docx import Document

from smartapply.cv import CvAdapter, CvDocxRenderer, HtmlApplicationRenderer
from smartapply.cv.assembly import assemble_cv
from smartapply.cv.role_family import classify
from smartapply.cv.skill_display import compact_sparse_secondary_categories
from smartapply.cv.skill_selection import normalize_skill_selection, restore_skill_selection_order
from smartapply.llm import AdaptedCV, CVSelection, JobAnalysis, SkillSelectionBlock
from smartapply.profile import get_profile


class _NoLLM:
    def complete_json(self, **kwargs):
        raise AssertionError("Skill preservation must not need a model call")


@pytest.mark.parametrize("role", ["Data Analyst", "Software Engineer", "LLM Engineer"])
def test_selected_catalogue_skills_survive_malformed_blocks_and_document_rendering(role):
    profile = get_profile()
    raw = CVSelection(
        cv_title="Applied AI Engineer",
        selected_project_ids=[],
        selected_certificate_ids=[],
        skills_profile_id="mixed",
        selected_skills=[
            SkillSelectionBlock(category_id="data_analysis", skills=["Python"]),
            SkillSelectionBlock(category_id="unknown_category", skills=["git", "Invented tool"]),
            SkillSelectionBlock(category_id="speech_audio", skills=["Pyannote"]),
            SkillSelectionBlock(category_id="data_analysis", skills=["SQL", "Python"]),
            SkillSelectionBlock(category_id="stats_signal", skills=["Mathematical optimization"]),
        ],
        skills_order=["speech_audio", "unknown_category", "data_analysis", "stats_signal"],
        warnings=[],
    )
    original = raw.model_dump()
    analysis = JobAnalysis(
        role_type=role,
        seniority="mid",
        domain="Technology",
        main_tasks=[],
        required_skills=[],
        nice_to_have=[],
        cv_keywords_to_include=[],
        match_reasons=[],
        risks=[],
    )
    adapter = CvAdapter(profile, llm=_NoLLM())
    selected = adapter._ensure_supported_offer_skills(assemble_cv(raw, profile), analysis)
    selected = adapter._apply_role_family_contract(selected, analysis, role)
    cv = AdaptedCV(**selected.model_dump(), professional_summary="Applied AI experience.")
    # Persist/reload as the real pipeline does: display ordering is not ephemeral.
    cv = AdaptedCV.model_validate_json(cv.model_dump_json())
    displayed, _merges = compact_sparse_secondary_categories(
        cv, primary_family=classify(analysis, title=role)
    )

    expected = {"Python", "SQL", "Git", "Pyannote", "Mathematical optimization"}
    canonical = {skill for block in selected.selected_skills for skill in block.skills}
    assert expected <= canonical <= profile.skills.allowed_skills
    assert {skill for block in displayed.selected_skills for skill in block.skills} == canonical
    assert all(block.category_id != "unknown_category" for block in selected.selected_skills)
    assert raw.model_dump() == original
    original_order = ["speech_audio", "data_infra", "data_analysis", "stats_signal"]
    assert cv.skills_order[:4] == displayed.skills_order[:4] == original_order
    assert [block.category_id for block in cv.llm_selected_skills] == original_order
    assert "llm_selected_skills" not in CVSelection.model_json_schema()["properties"]

    html = HtmlApplicationRenderer(profile).render_cv_html(displayed)
    html_skills = [
        unescape(skill.strip())
        for span in re.findall(r'<span class="skill-list">(.*?)</span>', html, re.S)
        for skill in span.split("·")
    ]
    document = Document()
    CvDocxRenderer(profile)._write_skills(document, displayed)
    docx_skills = [
        skill.strip()
        for paragraph in document.paragraphs
        for skill in paragraph.text.split(": ", 1)[1].split(", ")
    ]
    assert set(html_skills) == set(docx_skills) == canonical
    assert len(html_skills) == len(set(html_skills))
    assert len(docx_skills) == len(set(docx_skills))
    assert html_skills == docx_skills
    for block in displayed.selected_skills:
        initial = next((b for b in cv.llm_selected_skills if b.category_id == block.category_id), None)
        if initial:
            assert block.skills[:len(initial.skills)] == initial.skills


def test_repaired_category_keeps_its_original_skill_priority_when_merged():
    cv = AdaptedCV(
        cv_title="Data Analyst",
        professional_summary="Data analysis.",
        selected_project_ids=[],
        selected_experiences=[],
        selected_skills=[
            SkillSelectionBlock(category_id="data_analysis", skills=["Python"]),
            SkillSelectionBlock(category_id="unknown", skills=["SQL"]),
        ],
        skills_order=["unknown", "data_analysis"],
        warnings=[],
    )
    normalized = normalize_skill_selection(
        cv, allowed_skills_lower={"python", "sql"},
        supported_skills_by_category={"data_analysis": ["Python", "SQL"]},
    )
    final = restore_skill_selection_order(normalized, normalized.llm_selected_skills)

    assert normalized.selected_skills[0].skills == ["Python", "SQL"]
    assert final.skills_order == ["data_analysis"]
    assert final.selected_skills[0].skills == ["SQL", "Python"]
