"""CV titles must remain safe filenames while preserving readable Unicode."""

from pathlib import Path

from smartapply.pipeline.output_paths import application_document_stems


def test_document_names_sanitize_separators_and_controls_without_losing_accents():
    cv, letter = application_document_stems(
        "  Nour Lachtar  ", 'Ingénieur IA / Data (H/F)\n"Recherche"\\R&D\x00'
    )
    descriptor = "Nour Lachtar - Ingénieur IA - Data (H-F) -Recherche--R&D"
    assert cv == f"CV - {descriptor}"
    assert letter == f"Motivation - {descriptor}"
    for stem in (cv, letter):
        assert Path(stem).name == stem
        assert not any(char in stem for char in '<>:"/\\|?*\x00\n')


def test_long_unicode_offer_titles_fit_all_document_extensions():
    cv, letter = application_document_stems("Nour Lachtar", "Ingénieur " + "é" * 500)
    assert cv.removeprefix("CV - ") == letter.removeprefix("Motivation - ")
    for stem in (cv, letter):
        for extension in (".pdf", ".html", ".docx"):
            assert len((stem + extension).encode("utf-8")) <= 255


def test_empty_or_unsafe_components_do_not_create_an_empty_name():
    assert application_document_stems(" \n ", "../../") == (
        "CV - Candidate - CV", "Motivation - Candidate - CV"
    )
