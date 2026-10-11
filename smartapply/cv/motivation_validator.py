"""Light validation for generated motivation letters."""

from __future__ import annotations

import re

from smartapply.cv.text_hygiene import evidence_metadata_warnings
from smartapply.cv.validator import ValidationResult
from smartapply.llm import AdaptedCV, JobAnalysis, MotivationLetter
from smartapply.profile import Profile

_FOREIGN_SCRIPT_RANGES = {
    "arabic": r"\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF",
    "armenian": r"\u0530-\u058F",
    "cyrillic": r"\u0400-\u04FF",
    "hebrew": r"\u0590-\u05FF",
    "cjk": r"\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF",
}

DEFAULT_LETTER_MIN_WORDS = 180
DEFAULT_LETTER_MAX_WORDS = 240


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def _paragraph_count(text: str) -> int:
    paragraphs = re.split(r"\n\s*\n+", (text or "").strip())
    return len([paragraph for paragraph in paragraphs if paragraph.strip()])


def normalize_french_elisions(text: str, *, language: str = "fr") -> str:
    """Repair common missing French apostrophes without ASCII-folding text."""
    if language != "fr" or not text:
        return text

    def one_letter(match: re.Match[str]) -> str:
        prefix = match.group(1)
        word = match.group(2)
        return f"{prefix}'{word}"

    fixed = text
    # j ai -> j'ai, d AI -> d'AI, l IA -> l'IA, m ont -> m'ont, ...
    fixed = re.sub(
        r"(?<![A-Za-zÀ-ÖØ-öø-ÿ&-])\b([cdjlmnstCDJLMNST])\s+([A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ-]*)",
        one_letter,
        fixed,
    )

    phrase_replacements = (
        (r"\b[qQ]u\s+([A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ-]*)", "qu'{}"),
        (r"\b[jJ]usqu\s+([A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ-]*)", "jusqu'{}"),
        (r"\b[lL]orsqu\s+([A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ-]*)", "lorsqu'{}"),
        (r"\b[pP]uisqu\s+([A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ-]*)", "puisqu'{}"),
    )
    for pattern, replacement in phrase_replacements:
        fixed = re.sub(
            pattern,
            lambda m, repl=replacement: repl.format(m.group(1)),
            fixed,
        )
    fixed = re.sub(r"\b[Aa]ujourd\s+hui\b", "aujourd'hui", fixed)
    return fixed


_MISSING_APOSTROPHE_PATTERNS = (
    r"(?<![A-Za-zÀ-ÖØ-öø-ÿ&-])\b[cdjlmnstCDJLMNST]\s+[A-Za-zÀ-ÖØ-öø-ÿ]",
    r"\b[qQ]u\s+[A-Za-zÀ-ÖØ-öø-ÿ]",
    r"\b[jJ]usqu\s+[A-Za-zÀ-ÖØ-öø-ÿ]",
    r"\b[lL]orsqu\s+[A-Za-zÀ-ÖØ-öø-ÿ]",
    r"\b[pP]uisqu\s+[A-Za-zÀ-ÖØ-öø-ÿ]",
    r"\b[Aa]ujourd\s+hui\b",
)


def mentioned_project_ids(text: str, profile: Profile) -> list[str]:
    """Return profile project ids whose canonical name appears in text."""
    body_lower = _normalize(text)
    mentioned: list[str] = []
    for project in profile.projects:
        name = _normalize(project.name)
        if len(name) >= 4 and name in body_lower:
            mentioned.append(project.id)
    return mentioned


class MotivationLetterValidator:
    """Check letter structure, language hygiene and explicit project selection.

    Factual grounding and rhetorical judgments belong to the LLM review.
    A motivation letter need not repeat CV evidence, and lexical overlap cannot
    establish whether an attribution or expression of motivation is justified.
    """

    def __init__(
        self,
        profile: Profile,
        min_words: int = DEFAULT_LETTER_MIN_WORDS,
        max_words: int = DEFAULT_LETTER_MAX_WORDS,
    ):
        self.profile = profile
        self.min_words = min_words
        self.max_words = max_words

    def validate(
        self,
        letter: MotivationLetter,
        *,
        cv: AdaptedCV,
        analysis: JobAnalysis,
    ) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        body_lower = _normalize(letter.body)
        warnings.extend(
            evidence_metadata_warnings(
                {
                    "motivation_letter_subject": letter.subject,
                    "motivation_letter_body": letter.body,
                }
            )
        )

        for label in self._foreign_scripts(letter.body):
            errors.append(f"foreign_script_in_letter:{label}")

        paragraph_count = _paragraph_count(letter.body)
        if paragraph_count != 3:
            warnings.append(f"letter_not_3_paragraphs:{paragraph_count}")

        count = _word_count(letter.body)
        min_words = self.min_words
        max_words = self.max_words
        if count < min_words:
            warnings.append(f"letter_too_short:{count}")
        if count > max_words:
            warnings.append(f"letter_too_long:{count}")

        if (analysis.offer_language or "fr").lower().startswith("fr"):
            for pattern in _MISSING_APOSTROPHE_PATTERNS:
                if re.search(pattern, letter.body):
                    warnings.append("french_elision_missing_apostrophe")
                    break

        for project_id in self._unselected_projects_mentioned(body_lower, cv):
            warnings.append(f"unselected_project_in_letter:{project_id}")

        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)

    def _foreign_scripts(self, text: str) -> list[str]:
        found: list[str] = []
        for label, char_range in _FOREIGN_SCRIPT_RANGES.items():
            if re.search(f"[{char_range}]", text or ""):
                found.append(label)
        return found

    def _unselected_projects_mentioned(self, body_lower: str, cv: AdaptedCV) -> list[str]:
        selected = set(cv.selected_project_ids)
        return [
            project_id
            for project_id in mentioned_project_ids(body_lower, self.profile)
            if project_id not in selected
        ]
