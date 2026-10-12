"""Turn stored document diagnostics into short, safe product messages.

The persisted strings are an audit trail. Some include generated passages,
provider explanations or exceptions, so none of their free text belongs in
the desktop presentation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypedDict

from smartapply.cv.motivation_validator import (
    DEFAULT_LETTER_MAX_WORDS,
    DEFAULT_LETTER_MIN_WORDS,
)


class ReviewPoint(TypedDict):
    key: str
    label: str
    detail: str


class DocumentReviewPresentation(TypedDict):
    points: tuple[ReviewPoint, ...]
    corrected: bool


@dataclass(frozen=True)
class _Candidate:
    point: ReviewPoint
    priority: int


_WRAPPERS = (
    "validation_error:",
    "text_review_structural_error:",
    "text_review_invalid_correction:",
)

_FIELD_PHRASES = {
    "cv_title": "du titre du CV",
    "professional_summary": "du résumé du CV",
    "motivation_letter_subject": "de l’objet de la lettre",
    "motivation_letter_body": "du corps de la lettre",
}

_KNOWN_VALIDATION_ERRORS = {
    "publisher_reintroduced": (
        "document_company",
        "Nom de l’employeur à vérifier",
        "Vérifiez que les documents nomment bien l’employeur et non la plateforme de publication.",
    ),
    "experience_order_mismatch": (
        "cv_experience_order",
        "Ordre des expériences du CV",
        "Comparez l’ordre des expériences au profil source.",
    ),
    "unknown_experience_id": (
        "cv_experience_reference",
        "Expérience du CV à vérifier",
        "Une expérience ne correspond pas au profil source.",
    ),
    "experience_bullets_mismatch": (
        "cv_bullet_selection",
        "Descriptions d’expérience à vérifier",
        "Comparez les descriptions retenues au profil source.",
    ),
    "unknown_bullet_id": (
        "cv_bullet_reference",
        "Description d’expérience à vérifier",
        "Une description ne correspond pas au profil source.",
    ),
    "bullet_wrong_parent": (
        "cv_bullet_parent",
        "Description mal placée dans le CV",
        "Vérifiez à quelle expérience appartient cette description.",
    ),
    "experience_text_mismatch": (
        "cv_experience_text",
        "Texte d’expérience à vérifier",
        "Comparez le texte d’une expérience au profil source.",
    ),
    "too_few_projects": (
        "projects_minimum",
        "Projets du CV à compléter",
        "Le CV ne présente pas assez de projets sélectionnés.",
    ),
    "too_many_projects": (
        "projects_maximum",
        "Projets du CV à réduire",
        "Le CV présente trop de projets sélectionnés.",
    ),
    "duplicate_project_id": (
        "projects_duplicate",
        "Projet répété dans le CV",
        "Vérifiez la sélection des projets.",
    ),
    "unknown_project_id": (
        "projects_unknown",
        "Projet du CV à vérifier",
        "Un projet ne correspond pas au profil source.",
    ),
    "disabled_project_id": (
        "projects_disabled",
        "Projet du CV à retirer",
        "Un projet sélectionné n’est pas autorisé dans le profil.",
    ),
    "too_many_specialized_projects": (
        "projects_specialized",
        "Projets spécialisés à réduire",
        "Vérifiez le nombre de projets spécialisés sélectionnés.",
    ),
    "project_reason_count": (
        "projects_reasons_count",
        "Justification des projets à vérifier",
        "Vérifiez la justification de chaque projet sélectionné.",
    ),
    "unselected_project_reason": (
        "projects_reason_selection",
        "Justification de projet à vérifier",
        "Une justification concerne un projet non sélectionné.",
    ),
    "missing_project_reason": (
        "projects_reason_missing",
        "Justification de projet manquante",
        "Précisez pourquoi ce projet correspond à l’offre.",
    ),
    "ungrounded_project_evidence": (
        "projects_evidence",
        "Appui du projet à vérifier",
        "Rapprochez la justification du projet des éléments de l’offre.",
    ),
    "missing_activation_conditions": (
        "projects_conditions",
        "Conditions du projet à vérifier",
        "Vérifiez les conditions de sélection de ce projet spécialisé.",
    ),
    "missing_complementary_value": (
        "projects_complement",
        "Apport du projet à préciser",
        "Précisez la valeur complémentaire de ce projet spécialisé.",
    ),
    "too_many_certificates": (
        "certificates_maximum",
        "Certifications du CV à réduire",
        "Le CV présente trop de certifications sélectionnées.",
    ),
    "duplicate_certificate_id": (
        "certificates_duplicate",
        "Certification répétée dans le CV",
        "Vérifiez la sélection des certifications.",
    ),
    "unknown_certificate_id": (
        "certificates_unknown",
        "Certification du CV à vérifier",
        "Une certification ne correspond pas au profil source.",
    ),
}


def _unwrap(raw: str) -> tuple[str, tuple[str, ...]]:
    value = raw.strip()
    wrappers: list[str] = []
    while True:
        prefix = next((prefix for prefix in _WRAPPERS if value.startswith(prefix)), None)
        if prefix is None:
            return value, tuple(wrappers)
        wrappers.append(prefix)
        value = value[len(prefix) :].strip()


def _priority(wrappers: tuple[str, ...]) -> int:
    # A rejected revision describes a candidate that was never published.
    if "text_review_invalid_correction:" in wrappers:
        return 10
    if "text_review_structural_error:" in wrappers:
        return 20
    if "validation_error:" in wrappers:
        return 30
    return 40


def _count(value: str, prefix: str) -> int | None:
    match = re.fullmatch(re.escape(prefix) + r"(\d{1,5})", value)
    return int(match.group(1)) if match else None


def _point(key: str, label: str, detail: str, priority: int) -> _Candidate:
    return _Candidate({"key": key, "label": label, "detail": detail}, priority)


def _objective_point(value: str, priority: int) -> _Candidate | None:
    if value.startswith("letter_too_long:"):
        words = _count(value, "letter_too_long:")
        detail = (
            f"La lettre compte {words} mots ; la limite est de {DEFAULT_LETTER_MAX_WORDS}. "
            "Raccourcissez-la avant l’envoi."
            if words is not None
            else "La lettre dépasse la longueur prévue. Raccourcissez-la avant l’envoi."
        )
        return _point("letter_length", "Lettre trop longue", detail, priority)
    if value.startswith("letter_too_short:"):
        words = _count(value, "letter_too_short:")
        detail = (
            f"La lettre compte {words} mots ; {DEFAULT_LETTER_MIN_WORDS} sont attendus au minimum. "
            "Complétez-la avant l’envoi."
            if words is not None
            else "La lettre est plus courte que prévu. Complétez-la avant l’envoi."
        )
        return _point("letter_min_length", "Lettre trop courte", detail, priority)
    if value.startswith("letter_not_3_paragraphs:"):
        paragraphs = _count(value, "letter_not_3_paragraphs:")
        detail = (
            f"La lettre compte {paragraphs} paragraphes ; 3 sont attendus. Vérifiez sa structure."
            if paragraphs is not None
            else "La lettre doit comporter 3 paragraphes. Vérifiez sa structure."
        )
        return _point("letter_paragraphs", "Structure de la lettre", detail, priority)
    if value == "cv_title_not_offer_anchored":
        return _point(
            "cv_title_alignment",
            "Titre du CV à vérifier",
            "Rapprochez le titre du CV du poste visé avant l’envoi.",
            priority,
        )
    if value == "summary_not_offer_anchored":
        return _point(
            "summary_alignment",
            "Résumé du CV à vérifier",
            "Reliez le résumé aux missions ou compétences de l’offre.",
            priority,
        )
    if value.startswith("summary_too_long:"):
        length = _count(value, "summary_too_long:len=")
        detail = (
            f"Le résumé compte {length} caractères. Raccourcissez-le avant l’envoi."
            if length is not None
            else "Le résumé dépasse la longueur prévue. Raccourcissez-le avant l’envoi."
        )
        return _point("summary_length", "Résumé trop long", detail, priority)
    if value.startswith("summary_too_many_lines:"):
        lines = _count(value, "summary_too_many_lines:lines=")
        detail = (
            f"Le résumé occupe {lines} lignes. Vérifiez sa mise en page."
            if lines is not None
            else "Le résumé occupe trop de lignes. Vérifiez sa mise en page."
        )
        return _point("summary_lines", "Résumé sur trop de lignes", detail, priority)
    if value == "french_elision_missing_apostrophe":
        return _point(
            "letter_elision",
            "Orthographe de la lettre à vérifier",
            "Relisez les apostrophes et les élisions en français.",
            priority,
        )
    if value.startswith("unselected_project_in_letter:"):
        return _point(
            "letter_project",
            "Projet cité dans la lettre à vérifier",
            "Confirmez que ce projet figure parmi ceux retenus dans le CV.",
            priority,
        )
    if value.startswith("internal_evidence_metadata:"):
        return _point(
            "internal_terms",
            "Formulation interne à retirer",
            "Un terme de contrôle interne apparaît dans les documents. Reformulez ce passage.",
            priority,
        )
    if value.startswith("foreign_script_in_letter:"):
        return _point(
            "letter_foreign_script",
            "Langue de la lettre à vérifier",
            "Un passage utilise une écriture inattendue. Relisez la lettre.",
            priority,
        )
    if value.startswith("pdf_generation_skipped:"):
        return _point(
            "pdf_generation",
            "PDF à régénérer",
            "La création des PDF n’a pas abouti. Utilisez les fichiers disponibles ou régénérez le dossier.",
            priority,
        )
    if value == "cv_pdf_not_generated":
        return _point(
            "cv_pdf",
            "PDF du CV indisponible",
            "Régénérez le dossier pour obtenir le CV en PDF.",
            priority,
        )
    if value == "letter_pdf_not_generated":
        return _point(
            "letter_pdf",
            "PDF de la lettre indisponible",
            "Régénérez le dossier pour obtenir la lettre en PDF.",
            priority,
        )
    skill_points = {
        "unknown_skill_profile": "cv_skill_profile",
        "unknown_skill_category": "cv_skill_category",
        "unknown_selected_skill_category": "cv_selected_skill_category",
        "unknown_selected_skill": "cv_selected_skill",
    }
    skill_code = value.split(":", 1)[0]
    if skill_code in skill_points:
        return _point(
            skill_points[skill_code],
            "Compétences du CV à vérifier",
            "Vérifiez que les compétences affichées correspondent au profil validé.",
            priority,
        )
    if value.startswith("hallucinated_number:"):
        return _point(
            "cv_number",
            "Chiffres du CV à vérifier",
            "Comparez les chiffres d’une expérience du CV au profil source.",
            priority,
        )
    if value.startswith("low_text_overlap:"):
        return _point(
            "cv_text_overlap",
            "Texte d’expérience à vérifier",
            "Comparez le texte d’une expérience du CV au profil source.",
            priority,
        )
    if value.startswith("off_allowed_claims:"):
        return _point(
            "cv_claim_alignment",
            "Affirmations du CV à vérifier",
            "Comparez les affirmations d’une expérience du CV aux faits du profil source.",
            priority,
        )
    if value.startswith("bullet_too_long:"):
        return _point(
            "cv_bullet_length",
            "Descriptions d’expérience à raccourcir",
            "Raccourcissez les descriptions concernées du CV avant l’envoi.",
            priority,
        )
    return None


def _known_validation_point(value: str, priority: int) -> _Candidate | None:
    code = value.split(":", 1)[0].split(" ", 1)[0]
    if code == "empty_field":
        field = value.split(":", 1)[1] if ":" in value else ""
        if field in _FIELD_PHRASES:
            return _point(
                f"empty_field:{field}",
                "Champ du dossier à compléter",
                f"Le texte {_FIELD_PHRASES[field]} est vide. Complétez-le avant l’envoi.",
                priority,
            )
        return _point(
            "empty_field:other",
            "Champ du dossier à compléter",
            "Un champ du CV ou de la lettre est vide. Vérifiez le dossier avant l’envoi.",
            priority,
        )
    copy = _KNOWN_VALIDATION_ERRORS.get(code)
    if copy is None:
        return None
    return _point(*copy, priority)


def _review_point(value: str, priority: int) -> _Candidate | None:
    if value.startswith("text_review:"):
        _, category, field, *_ = value.split(":", 3) + [""]
        field = field if field in _FIELD_PHRASES else "other"
        field_phrase = _FIELD_PHRASES.get(field, "du texte du dossier")
        if category == "factual":
            return _point(
                f"factual:{field}",
                "Affirmation à vérifier",
                f"Confrontez une affirmation {field_phrase} aux sources avant l’envoi.",
                priority,
            )
        if category == "language":
            return _point(
                f"language:{field}",
                "Formulation à vérifier",
                f"Relisez la langue et la formulation {field_phrase}.",
                priority,
            )
        if category == "structure":
            return _point(
                f"structure:{field}",
                "Présentation à vérifier",
                f"Vérifiez la structure {field_phrase} avant l’envoi.",
                priority - 1,
            )
        if category == "style":
            return _point(
                f"style:{field}",
                "Suggestion de style",
                f"Une suggestion concerne la présentation {field_phrase}. Examinez-la si utile.",
                priority,
            )
    if value.startswith("text_review_unavailable:"):
        return _point(
            "review_status",
            "Relecture automatique indisponible",
            "Le contrôle n’a pas abouti. Relisez le CV et la lettre avant utilisation.",
            priority,
        )
    if value.startswith("text_review_incomplete:"):
        return _point(
            "review_status",
            "Relecture automatique incomplète",
            "Le contrôle n’est pas terminé. Relisez le CV et la lettre avant utilisation.",
            priority,
        )
    if value.startswith("text_review_unresolved:"):
        return _point(
            "review_status",
            "Relecture à terminer",
            "Un point reste sans correction confirmée. Relisez le texte conservé.",
            priority,
        )
    if value.startswith("text_review_"):
        return _point(
            "review_status",
            "Contrôle du texte à vérifier",
            "La relecture automatique n’a pas pu confirmer le texte. Relisez les documents.",
            priority - 1,
        )
    return None


def _plural_review_point(candidate: _Candidate, count: int) -> _Candidate:
    key = candidate.point["key"]
    category, _, field = key.partition(":")
    field_phrase = _FIELD_PHRASES.get(field, "du texte du dossier")
    if category == "factual":
        label = f"{count} affirmations à vérifier"
        detail = f"Confrontez ces affirmations {field_phrase} aux sources avant l’envoi."
    elif category == "language":
        label = f"{count} formulations à vérifier"
        detail = f"Relisez ces formulations {field_phrase} avant l’envoi."
    elif category == "structure":
        label = f"{count} points de structure à vérifier"
        detail = f"Vérifiez la structure {field_phrase} avant l’envoi."
    else:
        label = f"{count} suggestions de style"
        detail = (
            f"Des suggestions concernent la présentation {field_phrase}. Examinez-les si utile."
        )
    return _point(key, label, detail, candidate.priority)


def present_document_review(messages: Iterable[str] | None) -> DocumentReviewPresentation:
    """Present active issues once, keeping verified historical fixes separate.

    Labels and details are entirely controlled by this module. Stored free text,
    excerpts, source IDs and exception messages never cross the UI boundary.
    """
    selected: dict[str, _Candidate] = {}
    review_findings: dict[str, set[str]] = {}
    unknown_families: dict[str, str] = {}
    unknown_values: dict[str, set[str]] = {}
    unconfirmed_revision_keys: list[str] = []
    review_states: set[str] = set()
    rejected_correction = False
    corrected = False
    for raw in messages or ():
        value, wrappers = _unwrap(str(raw))
        if value.startswith("text_review_corrected:") and "validation_error:" not in wrappers:
            corrected = True
            continue
        if "validation_error:" not in wrappers and (
            value.startswith("auto_fixed:")
            or value == "letter_shortened_locally_then_reviewed"
            or value.startswith(
                (
                    "document_language_selected:",
                    "document_language_detected:",
                    "document_language_default:",
                )
            )
        ):
            continue
        priority = _priority(wrappers)
        if value.startswith("text_review_"):
            review_states.add(value.split(":", 1)[0])
        candidate = (
            _objective_point(value, priority)
            or _known_validation_point(value, priority)
            or _review_point(value, priority)
        )
        if any(
            wrapper in wrappers
            for wrapper in (
                "text_review_invalid_correction:",
                "text_review_structural_error:",
            )
        ):
            # These may describe a proposed revision that was not published.
            # Their counts are not necessarily those of the retained text.
            unconfirmed_revision_keys.append(candidate.point["key"] if candidate else "unknown")
            rejected_correction |= "text_review_invalid_correction:" in wrappers
            if "text_review_structural_error:" in wrappers:
                review_states.add("text_review_structural_error")
            continue
        if candidate is None:
            family_match = re.match(r"[a-z][a-z0-9_]*", value)
            family = family_match.group() if family_match else value
            unknown_number = len(unknown_families) + 1
            key = unknown_families.setdefault(
                family, "other" if unknown_number == 1 else f"other_{unknown_number}"
            )
            unknown_values.setdefault(key, set()).add(value)
            candidate = _point(
                key,
                "Contrôle supplémentaire à vérifier",
                "Relisez le CV et la lettre avant de les utiliser.",
                priority,
            )
        key = candidate.point["key"]
        if value.startswith("text_review:") and key.split(":", 1)[0] in {
            "factual",
            "language",
            "structure",
            "style",
        }:
            review_findings.setdefault(key, set()).add(value)
        existing = selected.get(key)
        if existing is None or candidate.priority > existing.priority:
            selected[key] = candidate

    # A free-form review reason can describe another cause than a validator
    # warning on the same field. Keep both unless their exact finding repeats.
    for key, findings in review_findings.items():
        if len(findings) > 1 and key in selected:
            selected[key] = _plural_review_point(selected[key], len(findings))
    for key, values in unknown_values.items():
        if len(values) > 1:
            selected[key] = _point(
                key,
                f"{len(values)} contrôles supplémentaires à vérifier",
                "Relisez le CV et la lettre avant de les utiliser.",
                selected[key].priority,
            )

    if rejected_correction or any(key not in selected for key in unconfirmed_revision_keys):
        selected.setdefault(
            "review_status",
            _point(
                "review_status",
                "Correction proposée à revoir"
                if rejected_correction
                else "Structure du document à vérifier",
                "La modification automatique n’a pas été retenue. Relisez le texte conservé."
                if rejected_correction
                else "Le contrôle de structure n’a pas abouti. Relisez le texte conservé.",
                10,
            ),
        )

    status = selected.get("review_status")
    if status is not None:
        details: list[str] = []
        if "text_review_unavailable" in review_states or "text_review_incomplete" in review_states:
            details.append("La relecture automatique n’a pas été terminée.")
        if "text_review_unresolved" in review_states:
            details.append("Une anomalie reste sans correction confirmée.")
        if rejected_correction:
            details.append("Une correction proposée a été écartée ; le texte initial est conservé.")
        if "text_review_structural_error" in review_states:
            details.append("Le contrôle de structure n’a pas abouti.")
        if review_states - {
            "text_review_unavailable",
            "text_review_incomplete",
            "text_review_unresolved",
            "text_review_structural_error",
        }:
            details.append("Le résultat de la relecture n’a pas pu être validé.")
        selected["review_status"] = _point(
            "review_status",
            status.point["label"],
            " ".join(details) if details else status.point["detail"],
            status.priority,
        )

    return {
        "points": tuple(candidate.point for candidate in selected.values()),
        "corrected": corrected,
    }
