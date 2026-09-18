"""Light validation for generated motivation letters."""

from __future__ import annotations

import re

from smartapply.cv.validator import ValidationResult
from smartapply.llm import AdaptedCV, JobAnalysis, MotivationLetter
from smartapply.profile import Profile

_SELF_DEPRECATION_PATTERNS = {
    "although_gap": r"\b(although|even though|while)\b[^.]{0,120}\b(i|my|this profile)\b[^.]{0,120}\b(do not|don't|does not|lack|lacks|limited|still learning|new to)\b",
    "no_experience_en": r"\b(i|my profile)\b[^.]{0,80}\b(do not|don't|have not|haven't|lack|lacks)\b[^.]{0,80}\b(experience|knowledge|expertise)\b",
    "limited_knowledge_en": r"\b(limited knowledge|limited experience|still learning|ready to learn|willing to learn|learning curve)\b",
    "aware_gap_en": r"\b(i am aware|i recognize|i acknowledge|i understand)\b[^.]{0,140}\b(requires?|may require|could require|limited|lack|gap)\b",
    "bien_que_fr": r"\b(bien que|même si)\b[^.]{0,160}\b(je|mon profil|ma connaissance|mon expérience)\b[^.]{0,160}\b(n['’]ai[e]? pas|soit limitée?|reste limitée?|manque|début de carrière|dois apprendre)\b",
    "je_nai_pas_fr": r"\bje n['’]ai[e]? pas\b[^.]{0,120}\b(expérience|connaissance|expertise|encore)\b",
    "limite_fr": r"\b(connaissance limitée|expérience limitée|expérience directe limitée|domaine que je n['’]ai pas encore exploré)\b",
    "aware_gap_fr": r"\b(je suis conscient|je reconnais|je comprends)\b[^.]{0,160}\b(nécessite|requiert|pourrait|peut nécessiter|limitée?|manque|plus approfondie)\b",
    "ready_to_learn_fr": r"\b(prêt(?:e)? à apprendre|disposé(?:e)? à apprendre|m['’]adapter et à apprendre|en train de me former)\b",
}

_FOREIGN_SCRIPT_RANGES = {
    "arabic": r"\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF",
    "armenian": r"\u0530-\u058F",
    "cyrillic": r"\u0400-\u04FF",
    "hebrew": r"\u0590-\u05FF",
    "cjk": r"\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF",
}


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
    """Check letter structure, language hygiene and selected-evidence references.

    Factual grounding is instructed in the generation prompts. Display skill
    names and offer-keyword overlap cannot establish whether a claim is true.
    """

    def __init__(self, profile: Profile, min_words: int = 220, max_words: int = 300):
        self.profile = profile
        self.min_words = min_words
        self.max_words = max_words
        self.projects_by_id = {p.id: p for p in profile.projects}
        self.experiences_by_id = {e.id: e for e in profile.experiences}

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

        for label, pattern in _SELF_DEPRECATION_PATTERNS.items():
            if re.search(pattern, body_lower, flags=re.IGNORECASE):
                warnings.append(f"letter_self_deprecation:{label}")

        if (analysis.offer_language or "fr").lower().startswith("fr"):
            for pattern in _MISSING_APOSTROPHE_PATTERNS:
                if re.search(pattern, letter.body):
                    warnings.append("french_elision_missing_apostrophe")
                    break

        for project_id in self._unselected_projects_mentioned(body_lower, cv):
            warnings.append(f"unselected_project_in_letter:{project_id}")

        if not self._references_selected_evidence(body_lower, cv):
            warnings.append("letter_may_not_reference_selected_evidence")

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

    def _references_selected_evidence(self, body_lower: str, cv: AdaptedCV) -> bool:
        evidence_terms: set[str] = set()
        for project_id in cv.selected_project_ids:
            project = self.projects_by_id.get(project_id)
            if project:
                evidence_terms.add(project.name)
                evidence_terms.update(project.keywords[:4])
            evidence_terms.add(project_id)

        for exp in cv.selected_experiences:
            source = self.experiences_by_id.get(exp.source_id)
            if source:
                evidence_terms.add(source.company)
                evidence_terms.add(source.title)
                evidence_terms.update(source.keywords[:4])
            evidence_terms.add(exp.source_id)
            for bullet in exp.bullets:
                evidence_terms.add(bullet.source_id)

        normalized_terms = [
            _normalize(term) for term in evidence_terms if len(_normalize(term)) >= 4
        ]
        return any(term in body_lower for term in normalized_terms)
