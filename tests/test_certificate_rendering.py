"""The finalized certificate selection controls every CV export."""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from smartapply.cv import CvDocxRenderer, HtmlApplicationRenderer
from smartapply.llm import AdaptedCV
from smartapply.profile import get_profile


def _cv(selected_ids: list[str] | None) -> AdaptedCV:
    return AdaptedCV(
        cv_title="Data Scientist",
        professional_summary="Applied machine learning.",
        selected_experiences=[],
        selected_project_ids=[],
        selected_certificate_ids=selected_ids,
        skills_order=[],
        warnings=[],
    )


@pytest.mark.parametrize("count", [0, 1, 2])
def test_html_and_docx_show_only_selected_certificates(count):
    profile = get_profile()
    chosen = list(reversed(profile.certificates))[:count]
    cv = _cv([certificate.id for certificate in chosen])

    html = HtmlApplicationRenderer(profile).render_cv_html(cv)
    section = BeautifulSoup(html, "html.parser").select_one("section.certificates")
    document = CvDocxRenderer(profile).render(cv)
    paragraphs = [paragraph.text for paragraph in document.paragraphs]

    assert (section is not None) == bool(chosen)
    assert ("Certificates" in paragraphs) == bool(chosen)
    if section is not None:
        assert [row.strong.get_text(strip=True) for row in section.select("p")] == [
            certificate.name for certificate in chosen
        ]
    certificate_lines = [
        paragraph
        for paragraph in paragraphs
        if any(certificate.name in paragraph for certificate in profile.certificates)
    ]
    assert certificate_lines == [
        f"{certificate.name} ↗ — {certificate.issuer} — {certificate.description}"
        for certificate in chosen
    ]


def test_zero_certificates_are_absent_from_pdf_source(monkeypatch, tmp_path: Path):
    import smartapply.cv.html_renderer as html_renderer

    captured = []
    output = tmp_path / "cv.pdf"

    def capture(html, path, *, base_dir):
        captured.append(html)
        return Path(path)

    monkeypatch.setattr(html_renderer, "html_to_pdf", capture)
    monkeypatch.setattr(html_renderer, "pdf_page_count", lambda path: 1)

    HtmlApplicationRenderer(get_profile()).save_cv_pdf(_cv([]), output)

    assert len(captured) == 1
    assert BeautifulSoup(captured[0], "html.parser").select_one("section.certificates") is None


def test_legacy_cv_without_selection_keeps_existing_certificates():
    profile = get_profile()
    legacy = _cv(None).model_dump(exclude={"selected_certificate_ids"})
    restored = AdaptedCV.model_validate(legacy)

    assert restored.selected_certificate_ids is None
    html = HtmlApplicationRenderer(profile).render_cv_html(restored)
    assert [
        row.strong.get_text(strip=True)
        for row in BeautifulSoup(html, "html.parser").select("section.certificates p")
    ] == [certificate.name for certificate in profile.certificates]
    paragraphs = [
        paragraph.text for paragraph in CvDocxRenderer(profile).render(restored).paragraphs
    ]
    assert "Certificates" in paragraphs
