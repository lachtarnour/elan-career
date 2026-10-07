"""Tests for the parsing module."""

from __future__ import annotations

from smartapply.parsing import (
    clean_description,
    extract_sections,
    strip_html,
)


def test_strip_html_removes_tags_and_scripts() -> None:
    html = "<p>Hello <b>world</b><script>alert(1)</script></p>"
    out = strip_html(html)
    assert "alert" not in out
    assert "Hello" in out and "world" in out


def test_strip_html_keeps_inline_formatted_ml_missions_together() -> None:
    html = (
        "<p>Développer des modèles de <strong>Machine Learning</strong> "
        "avec <em>PyTorch</em> et <a href='/tensorflow'>TensorFlow</a>.</p>"
        "<p>Entraîner et déployer des modèles de <span>Deep Learning</span>.</p>"
    )

    assert strip_html(html) == (
        "Développer des modèles de Machine Learning avec PyTorch et TensorFlow.\n"
        "Entraîner et déployer des modèles de Deep Learning."
    )


def test_strip_html_preserves_block_list_and_line_break_boundaries() -> None:
    html = (
        "<h2>Missions</h2><div><p>Concevoir des modèles.</p>"
        "<ul><li>Entraîner les modèles.</li><li>Évaluer les résultats.<br>"
        "Déployer en production.</li></ul></div>"
        "<h2>Profil</h2><p>Python et SQL.</p>"
    )

    assert strip_html(html).splitlines() == [
        "Missions",
        "Concevoir des modèles.",
        "Entraîner les modèles.",
        "Évaluer les résultats.",
        "Déployer en production.",
        "Profil",
        "Python et SQL.",
    ]


def test_strip_html_preserves_adjacent_inline_text_and_punctuation() -> None:
    html = (
        "<p><span>Py</span><strong>Torch</strong>, "
        "<em>scikit</em>-<span>learn</span> et <a href='/ml'>ML</a>.</p>"
    )

    assert strip_html(html) == "PyTorch, scikit-learn et ML."


def test_strip_html_decodes_entities_and_removes_hidden_content() -> None:
    html = (
        "<p>R&amp;D&nbsp;: <strong>IA</strong> &lt;modèles&gt;.</p>"
        "<style>.hidden { color: red; }</style><script>alert('script')</script>"
        "<noscript>fallback</noscript><iframe>embedded</iframe><svg>vector</svg>"
    )

    assert strip_html(html) == "R&D : IA <modèles>."


def test_clean_description_reduces_size_meaningfully() -> None:
    noisy = (
        "<p>Looking for a <b>Data Scientist</b>.</p>\n"
        "Equal opportunity employer.\n"
        "Reasonable accommodation will be provided.\n"
        "Compensation: $100k\n"
        "About us\n"
        "We are an amazing company with great values.\n"
        "Responsibilities\n"
        "- Build ML pipelines"
    )
    cleaned = clean_description(noisy)
    assert "Data Scientist" in cleaned
    assert "Build ML pipelines" in cleaned
    assert "Equal opportunity" not in cleaned
    assert "great values" not in cleaned
    assert "<p>" not in cleaned


def test_extract_sections_finds_responsibilities_and_profile() -> None:
    text = (
        "Missions\n"
        "Build RAG pipelines\n"
        "Deploy to AWS\n\n"
        "Profil recherché\n"
        "3+ years Python\n"
        "PyTorch experience\n\n"
        "Avantages\n"
        "Mutuelle, tickets restau"
    )
    sections = extract_sections(text)
    assert "responsibilities" in sections
    assert "RAG" in sections["responsibilities"]
    assert "profile" in sections
    assert "Python" in sections["profile"]
    assert "benefits" in sections
    assert "Mutuelle" in sections["benefits"]
