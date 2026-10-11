"""Cheap local offer-language detection shared by filtering and generation."""

_FRENCH_MARKERS = (
    "vos missions",
    "profil recherché",
    "profil recherche",
    "candidature",
    "poste",
    "maîtrise",
    "maitrise",
    "expérience",
    "développement",
    "developpement",
    "requis",
    "anglais courant",
    "cdi",
    "télétravail",
    "teletravail",
    "île-de-france",
)
_ENGLISH_MARKERS = (
    "responsibilities",
    "requirements",
    "apply",
    "role",
    "candidate",
    "experience required",
    "english",
)
_OTHER_LANGUAGE_MARKERS: dict[str, tuple[str, ...]] = {
    "de": (
        "wir suchen",
        "deine aufgaben",
        "ihre aufgaben",
        "anforderungen",
        "berufserfahrung",
        "kenntnisse",
        "bewerbung",
        "deutschkenntnisse",
    ),
    "es": (
        "buscamos",
        "tus funciones",
        "responsabilidades",
        "requisitos",
        "experiencia laboral",
        "conocimientos",
        "candidatura",
    ),
    "it": (
        "cerchiamo",
        "responsabilità",
        "responsabilita",
        "requisiti",
        "esperienza lavorativa",
        "competenze richieste",
        "candidatura",
    ),
    "nl": (
        "wij zoeken",
        "jouw taken",
        "verantwoordelijkheden",
        "vereisten",
        "werkervaring",
        "sollicitatie",
    ),
    "pt": (
        "procuramos",
        "suas responsabilidades",
        "requisitos",
        "experiência profissional",
        "experiencia profissional",
        "candidatura",
    ),
}


def detect_offer_language(text: str) -> str:
    """Return ``fr`` or ``en`` for generated recruiter-facing text."""
    normalized = (text or "").lower()
    french_score = sum(1 for marker in _FRENCH_MARKERS if marker in normalized)
    english_score = sum(1 for marker in _ENGLISH_MARKERS if marker in normalized)
    return "fr" if french_score >= english_score else "en"


def detect_offer_language_confident(text: str) -> str | None:
    """Return a language code only when local evidence is unambiguous.

    Filtering deliberately fails open: short or mixed-language offers are kept
    rather than archived because of a weak guess.
    """
    normalized = (text or "").lower()
    scores = {
        "fr": sum(1 for marker in _FRENCH_MARKERS if marker in normalized),
        "en": sum(1 for marker in _ENGLISH_MARKERS if marker in normalized),
        **{
            language: sum(1 for marker in markers if marker in normalized)
            for language, markers in _OTHER_LANGUAGE_MARKERS.items()
        },
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_language, best_score = ranked[0]
    next_score = ranked[1][1]
    if best_score < 2 or best_score <= next_score:
        return None
    return best_language


def resolve_document_language(
    offer_language: str | None, offer_text: str,
) -> tuple[str, str | None]:
    """Resolve a writing language without changing the extracted offer fact.

    Preserve concrete language values, including historical ones. For
    ``mixed_fr_en``, always write in French. ``other`` and historical
    ``unknown`` do not name a language:
    inspect the announcement locally, or use the French document default.
    The default is a writing choice, not a claim about the offer's language.
    """
    language = offer_language or "fr"
    sentinel = language.strip().lower()
    if sentinel == "mixed_fr_en":
        return "fr", f"document_language_selected:fr:offer_language_{sentinel}"
    if sentinel not in {"other", "unknown"}:
        return language, None
    detected = detect_offer_language_confident(offer_text)
    if detected is not None:
        return detected, f"document_language_detected:{detected}:offer_language_{sentinel}"
    return "fr", f"document_language_default:fr:offer_language_{sentinel}"


__all__ = [
    "detect_offer_language", "detect_offer_language_confident", "resolve_document_language",
]
