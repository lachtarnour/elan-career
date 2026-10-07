"""Generic evidence that the advertised candidate will perform AI/ML work.

This module does not classify an offer as relevant.  It only detects a
candidate-owned technical mission strongly enough to invalidate a title-only
off-target veto.  The later relevance stage still decides whether that work is
central enough for the candidate.
"""

from __future__ import annotations

import re

from smartapply.filtering.text import norm

_ACTION = (
    r"(?:"
    r"design(?:s|ed|ing)?|build(?:s|ing)?|built|create(?:s|d|ing)?|"
    r"develop(?:s|ed|ing|ment)?|implement(?:s|ed|ing|ation)?|"
    r"push(?:es|ed|ing)?|"
    r"integrat(?:e|es|ed|ing|ion)|deploy(?:s|ed|ing|ment)?|"
    r"train(?:s|ed|ing)?|evaluat(?:e|es|ed|ing|ion)|"
    r"operat(?:e|es|ed|ing|ion)|optimis(?:e|es|ed|ing|ation)|"
    r"optimiz(?:e|es|ed|ing|ation)|industrializ(?:e|es|ed|ing|ation)|"
    r"orchestrat(?:e|es|ed|ing|ion)|architect(?:s|ed|ing|ure)?|"
    r"prototype|prototyping|maintain(?:s|ed|ing)?|apply|applying|"
    r"imagin(?:e|es|ed|ing)|explor(?:e|es|ed|ing)|study|studying|"
    r"concev(?:oir|ez|ra|rez|ront)|conception|"
    r"construi(?:re|sez|ra|rez|ront)|construction|"
    r"cre(?:er|ez|era|erez|ation)|"
    r"developp(?:er|ez|e|era|erez|ement)|"
    r"implement(?:er|ez|era|erez|ation)|"
    r"integr(?:er|ez|era|erez|ation)|"
    r"deploy(?:er|ez|era|erez)|deploiement|"
    r"entrain(?:er|ez|era|erez|ement)|"
    r"evalu(?:er|ez|era|erez|ation)|"
    r"industrialis(?:er|ez|era|erez|ation)|"
    r"optimis(?:er|ez|era|erez|ation)|"
    r"orchestr(?:er|ez|era|erez|ation)|"
    r"architectur(?:er|ez|era|erez|e)|"
    r"prototyp(?:er|ez|era|erez|age)|"
    r"imagin(?:er|ez|era|erez)|explor(?:er|ez|era|erez)|etud(?:ier|iez|iera|ierez)|"
    r"mainten(?:ir|ez|ance)|appliqu(?:er|ez)|"
    r"mettre\s+en\s+production|mise\s+en\s+production|"
    r"contribu(?:er|ez|era|erez|tion)"
    r")"
)

_TARGET_OBJECT = (
    r"(?:"
    r"(?:machine\s+learning|deep\s+learning|apprentissage\s+"
    r"(?:automatique|profond)|ml|ai|ia)\s+models?|"
    r"modeles?\s+(?:(?:de|d[' ]?)\s*)?(?:machine\s+learning|deep\s+learning|"
    r"apprentissage\s+(?:automatique|profond)|ml|ai|ia)|"
    r"(?:models?|modeles?)\s+(?:d[' ]?)?(?:inference|prediction|langage)|"
    r"modeles?[^.!?;\n]{0,80}(?:machine\s+learning|deep\s+learning|ml|ia|ai)|"
    r"(?:inference|training|deployment|serving|evaluation)\s+pipelines?|"
    r"pipelines?\s+(?:d[' ]?)?(?:inference|entrainement|deploiement|evaluation)|"
    r"rag(?:\s+(?:pipeline|system|application|workflow)s?)?|"
    r"(?:ai|ia)[- ]powered\s+(?:solutions?|applications?|assistants?|systems?)|"
    r"(?:ai|ia)\s+(?:solutions?|applications?|assistants?|pipelines?|systems?|"
    r"logiciels?|produits?|tools?|platforms?)|"
    r"(?:solutions?|applications?|assistants?|pipelines?|systemes?|logiciels?)\s+"
    r"(?:ai|ia)|"
    r"llms?|large\s+language\s+models?|modeles?\s+de\s+langage|"
    r"(?:ai|ia|llm|intelligent)\s+agents?|"
    r"agents?\s+(?:ai|ia|llm|autonomes?|agentiques?)|agent\s+loops?|"
    r"agentic\s+(?:ai\s+)?systems?|systemes?\s+agentiques?|"
    r"computer\s+vision|vision\s+par\s+ordinateur|vision\s+industrielle|"
    r"traitement\s+(?:du\s+signal|d[' ]?images?)|image\s+processing|"
    r"signal\s+processing|feature\s+extraction|"
    r"algorithmes?[^.!?;\n]{0,80}(?:image|signal|vision|inference|prediction)|"
    r"(?:image|signal|vision|inference|prediction)[^.!?;\n]{0,80}algorithmes?|"
    r"neural\s+networks?|reseaux?\s+de\s+neurones|"
    r"methodes?\s+d[' ]?apprentissage(?:\s+profond)?|deep\s+learning\s+methods?|"
    r"methodes?\s+(?:d[' ]?)?(?:ai|ia)|"
    r"cas\s+d[' ]?usage\s+(?:ai|ia)|(?:ai|ia)\s+use\s+cases?|"
    r"algorithmes?\s+(?:de\s+)?(?:machine\s+learning|deep\s+learning|ml|ia|ai)|"
    r"modeles?\s+(?:predictifs?|sequentiels?)|predictive\s+models?|"
    r"nlp|natural\s+language\s+processing|traitement\s+(?:automatique\s+)?du\s+langage|"
    r"ml\s+inference|inference\s+ml|edge\s+ai|edge\s+ia|"
    r"(?:ai|ia)\s+(?:embarquee?|embedded)|embedded\s+(?:ai|ia)|"
    r"(?:ai|ia)[- ]native\s+(?:tools?|platforms?|systems?|software)|"
    r"mlops|model\s+registry|rl\s+environments?|"
    r"hallucinations?|prompt\s+injections?|llm[- ]as[- ]a[- ]judge"
    r")"
)

_ACTION_AND_OBJECT = re.compile(
    rf"(?:\b{_ACTION}\b.{{0,150}}\b{_TARGET_OBJECT}\b|"
    rf"\b{_TARGET_OBJECT}\b.{{0,150}}\b{_ACTION}\b)"
)
_DATA_FOR_AI_MISSION = re.compile(
    r"\b(?:structuration|preparation|alimentation)\b.{0,90}\b"
    r"(?:rag|bases?\s+vectorielles?|evaluation\s+de\s+modeles?|"
    r"modeles?\s+de\s+langage|agents?\s+(?:ia|ai))\b"
)
_CANDIDATE_FRAME = re.compile(
    r"(?:^missions?\s*:|\b(?:you\s+(?:will|shall|are\s+responsible)|"
    r"your\s+(?:role|mission|responsibilit(?:y|ies)|objective)|"
    r"responsibilit(?:y|ies)\s+(?:include|are)|what\s+you\s+will\s+do|"
    r"the\s+role\s+(?:involves|includes)|in\s+this\s+role|"
    r"vous\s+(?:allez|devez|devrez|serez|aurez)|vos\s+(?:missions|responsabilites)|"
    r"votre\s+(?:role|mission|objectif)|le\s+poste\s+(?:consiste|implique)|"
    r"les\s+missions\s+(?:incluent|sont)|en\s+tant\s+qu[' ]?|"
    r"vous|you|le\s+developpeur|la\s+developpeuse)\b)"
)
_COMPANY_FRAME = re.compile(
    r"\b(?:building\s+on|our\s+(?:company|business|product|platform)|"
    r"we\s+(?:design|build|develop|train|evaluate|integrate|deploy)|"
    r"company\s+(?:designs|builds|develops)|"
    r"notre\s+(?:entreprise|societe|produit|plateforme)|"
    r"nous\s+(?:concevons|construisons|developpons|entrainons|deployons)|"
    r"l\s+entreprise|la\s+societe)\b"
)
_INCIDENTAL_FRAME = re.compile(
    r"\b(?:nice\s+to\s+have|would\s+be\s+a\s+plus|preferred|familiarity\s+with|"
    r"knowledge\s+of|experience\s+(?:with|in|dans)|awareness\s+of|"
    r"serait\s+un\s+plus|connaissance\s+(?:de|des)|familiarite\s+avec|"
    r"experience\s+(?:avec|en|dans)|(?:connaissez|maitrisez)\s+(?:les|l\s+usage)|"
    r"(?:using|utilisant)\s+(?:daily|quotidiennement\s+)?(?:ai|ia)?\s*assistants?)\b"
)
_ACTION_AT_START = re.compile(
    rf"^[\s):>*-]*(?:proposer\s+et\s+|savoir\s+)?{_ACTION}\b"
)
_ACTION_AFTER_LABEL = re.compile(
    rf"^[^:\n]{{1,60}}:\s*(?:proposer\s+et\s+|savoir\s+)?{_ACTION}\b"
)
_HARDWARE_EXECUTION_CONTEXT = re.compile(
    r"\b(?:developpement|development|conception|design)\b.{0,60}\b"
    r"(?:puces?|chips?|hardware|accelerateurs?|accelerators?|capteurs?|sensors?)\b"
    r".{0,80}\b(?:executer|run(?:ning)?|inference)\b.{0,50}\b"
    r"(?:models?|modeles?|ai|ia|ml)\b"
)


def candidate_ai_mission(description: str) -> str | None:
    """Return one concrete candidate-owned AI/ML mission, if present."""

    for raw_clause in _mission_windows(description):
        clause = norm(raw_clause)
        if not (_ACTION_AND_OBJECT.search(clause) or _DATA_FOR_AI_MISSION.search(clause)):
            continue

        candidate_frame = _CANDIDATE_FRAME.search(clause) is not None
        if _COMPANY_FRAME.search(clause) and not candidate_frame:
            continue
        if _INCIDENTAL_FRAME.search(clause):
            continue
        if _HARDWARE_EXECUTION_CONTEXT.search(clause):
            continue
        if (
            candidate_frame
            or _DATA_FOR_AI_MISSION.search(clause)
            or _ACTION_AT_START.search(clause)
            or _ACTION_AFTER_LABEL.search(clause)
            or raw_clause.lstrip().startswith(("-", "*", "•"))
        ):
            return raw_clause.strip()
    return None


def _clauses(text: str) -> tuple[str, ...]:
    return tuple(
        clause.strip()
        for clause in re.split(r"(?:\r?\n|[.!?;•]+)", text)
        if clause.strip()
    )


def _mission_windows(text: str) -> tuple[str, ...]:
    """Return clauses plus only the adjacent fragments needed for markup.

    Job boards often put a bold object on the line after its action (for
    example ``Build`` then ``ML models``).  Pair adjacent fragments only when
    one contains an action and the other a target object; unrelated bullets
    are never merged.
    """

    clauses = _clauses(text)
    windows: list[str] = []
    action = re.compile(rf"\b{_ACTION}\b")
    target = re.compile(rf"\b{_TARGET_OBJECT}\b")
    for index, clause in enumerate(clauses):
        windows.append(clause)
        if index + 1 >= len(clauses):
            continue
        following = clauses[index + 1]
        if len(clause) + len(following) > 500:
            continue
        if (action.search(norm(clause)) and target.search(norm(following))) or (
            target.search(norm(clause)) and action.search(norm(following))
        ):
            windows.append(f"{clause} {following}")
    return tuple(windows)


__all__ = ["candidate_ai_mission"]
