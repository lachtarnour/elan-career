"""Shared, identified sources for writing and reviewing final document text.

Declared skills support general competence claims. Only experience/project
facts support attributing a tool, result or responsibility to a specific job
or project. The assembled CV is a document to inspect, not independent
evidence that its generated title or summary is true.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from smartapply.llm.prompts.loader import load_prompt
from smartapply.llm.schemas import AdaptedCV, AssembledCV, JobAnalysis
from smartapply.profile import Bullet, Profile

_CONTEXT_SOURCE_IDS = {
    "candidate:provided_evidence",
    "context:reference_date",
    "document:final_cv",
    "profile:identity",
    "profile:declared_skills",
    "offer:description",
    "offer:analysis",
    "style:title",
    "style:summary",
    "style:letter",
    "style:recruiter_text",
}


def _bullet_evidence(bullet: Bullet) -> dict[str, Any]:
    return {
        "source_id": f"bullet:{bullet.id}",
        "id": bullet.id,
        "text": bullet.text,
        "allowed_claims": list(bullet.effective_allowed_claims),
        "evidence_level": bullet.evidence_level,
        "numbers": list(bullet.numbers),
    }


def _candidate_source_ids(profile: Profile, adapted_cv: AdaptedCV | AssembledCV) -> set[str]:
    """Candidate declarations/facts actually exposed, excluding generated prose."""
    source_ids = {"profile:identity", "profile:declared_skills"}
    source_ids.update(f"skills:{category.id}" for category in profile.skills.categories)
    selected_experiences = {experience.source_id for experience in adapted_cv.selected_experiences}
    for experience in profile.experiences:
        if experience.id not in selected_experiences:
            continue
        source_ids.add(f"experience:{experience.id}")
        source_ids.update(f"bullet:{bullet.id}" for bullet in experience.bullets)
    selected_projects = set(adapted_cv.selected_project_ids)
    for project in profile.projects:
        if project.id not in selected_projects:
            continue
        source_ids.add(f"project:{project.id}")
        source_ids.update(f"bullet:{bullet.id}" for bullet in project.bullets)
    source_ids.update(f"education:{degree.id}" for degree in profile.education)
    source_ids.update(f"certificate:{certificate.id}" for certificate in profile.certificates)
    source_ids.update(f"language:{index}" for index, _ in enumerate(profile.languages))
    return source_ids


def allowed_source_ids(profile: Profile, adapted_cv: AdaptedCV | AssembledCV) -> set[str]:
    """Exactly the sources exposed by ``build_document_context``, including scope.

    Only selected projects/experiences have factual source IDs in this request.
    The aggregate identifies that supplied evidence, never the whole hidden
    profile or generated documents as independent proof.
    """
    return _CONTEXT_SOURCE_IDS | _candidate_source_ids(profile, adapted_cv)


def build_document_context(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    adapted_cv: AdaptedCV | AssembledCV,
    job_title: str,
    job_company: str,
    job_description: str,
    language: str = "fr",
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Build read-only final state and sources, without author reasoning."""
    experiences_by_id = {experience.id: experience for experience in profile.experiences}
    projects_by_id = {project.id: project for project in profile.projects}
    experiences = []
    for selected in adapted_cv.selected_experiences:
        source = experiences_by_id.get(selected.source_id)
        if source is None:
            continue
        experiences.append(
            {
                "source_id": f"experience:{source.id}",
                **source.model_dump(mode="json", exclude={"bullets", "keywords"}),
                "bullets": [_bullet_evidence(bullet) for bullet in source.bullets],
            }
        )
    projects = []
    for project_id in dict.fromkeys(adapted_cv.selected_project_ids):
        project = projects_by_id.get(project_id)
        if project is None:
            continue
        projects.append(
            {
                "source_id": f"project:{project.id}",
                **project.model_dump(
                    mode="json", exclude={"bullets", "keywords", "selection_policy"}
                ),
                "bullets": [_bullet_evidence(bullet) for bullet in project.bullets],
            }
        )
    style = profile.style_guide
    return {
        "candidate_identity": {
            "source_id": "profile:identity",
            "data": profile.identity.model_dump(mode="json"),
        },
        "declared_skills": {
            "source_id": "profile:declared_skills",
            "scope": (
                "All candidate-declared skills, including those not displayed in the final CV. "
                "Valid support for general competence claims without a mandatory project proof; "
                "not proof of a particular realization, employer, product or level of seniority."
            ),
            "categories": [
                {"source_id": f"skills:{category.id}", **category.model_dump(mode="json")}
                for category in profile.skills.categories
            ],
        },
        "languages": [
            {"source_id": f"language:{index}", **item.model_dump(mode="json")}
            for index, item in enumerate(profile.languages)
        ],
        "education": [
            {"source_id": f"education:{item.id}", **item.model_dump(mode="json")}
            for item in profile.education
        ],
        "certificates": [
            {"source_id": f"certificate:{item.id}", **item.model_dump(mode="json")}
            for item in profile.certificates
        ],
        "recruiter_text_style": {
            "source_id": "style:recruiter_text",
            "editorial_rules": load_prompt("shared/recruiter_text_rules.j2"),
            "scope": "Generated CV title, professional summary, letter subject and body.",
        },
        "title_style": {
            "source_id": "style:title",
            "language": "en",
            "purpose": (
                "A polished, stable professional headline under the candidate's name. "
                "Use a source-supported broad role, with a natural supported domain or "
                "specialization when useful. Prefer a simpler honest headline when a "
                "qualifier would be unsupported or awkward."
            ),
            "grounding": (
                "The headline expresses professional positioning; it need not be an exact "
                "historical job title. Assess support semantically from candidate identity, "
                "declarations and relevant facts. A declared skill supports competence "
                "without mandatory project proof, not an invented qualification or level "
                "of seniority. The offer alone cannot establish the candidate's role, "
                "seniority, specialization or credentials."
            ),
            "presentation": (
                "Do not turn the headline into a skills list, tool/stack suffix, compressed "
                "job description or mission statement. Keep it consistent with the summary "
                "and the letter's claims about the candidate, without requiring identical "
                "wording. A letter subject may still name a different target role."
            ),
            "scope": "CV cv_title only; never apply its language or form to the letter.",
        },
        "summary_style": {
            "source_id": "style:summary",
            "editorial_rules": load_prompt("cv_adaptation/summary_rules.j2"),
            "language": "en",
            "tone": style.tone,
            "voice": style.voice,
            "max_characters": style.max_summary_length,
            "max_lines": style.max_summary_lines,
            "scope": "CV professional_summary only; never apply its voice rules to the letter.",
        },
        "experience_evidence": experiences,
        "letter_style": {
            "source_id": "style:letter",
            "body_scope": (
                "The voice, opening, tone, audience, accessibility, paragraphs, word limits, "
                "concision, purpose, career_examples and personalization below apply only "
                "to motivation_letter_body (body in MotivationLetter), never to the subject."
            ),
            "voice": "first person: je in French, I in English",
            "opening": (
                "In the first sentence, connect a precise aspect of the role or organization's "
                "activity to the candidate's interest or supported background. Avoid "
                "administrative openings, compliments and merely restating the advertisement."
            ),
            "tone": (
                "Professional, warm and restrained, with short sentences and direct verbs. "
                "Express enthusiasm through specific reasons. Avoid stock formulations such "
                "as 'Fort de mon expérience', 'votre entreprise dynamique', 'mettre mes "
                "compétences au service de', and equivalents in the requested language."
            ),
            "audience": (
                "Recruiters and HR readers who may not be specialists in the candidate's "
                "technical field."
            ),
            "accessibility": (
                "Make the motivation and the relevance of each chosen example understandable "
                "without specialist knowledge. Keep precise technical names and useful details; "
                "explain their role in plain language where needed for understanding. Avoid "
                "jargon density that obscures the point, without replacing precise facts with "
                "vague claims or adding unsupported outcomes. Do not require a glossary or "
                "an explanation of every acronym. The number and detail of examples remain "
                "freely chosen within the letter's overall length."
            ),
            "paragraphs": 3,
            "min_words": 180,
            "max_words": 240,
            "concision": (
                "Keep the letter short and complete. Each sentence should add a specific "
                "motivation, a concrete candidate fact or a prospective contribution. Do not "
                "expand an idea or add generic praise merely to reach a word count."
            ),
            "purpose": (
                "Explain why the candidate wants to join this organization and do this work. "
                "Offer-specific motivation and prospective contribution lead the letter; "
                "the CV is a factual reference, not an outline to retell."
            ),
            "career_examples": (
                "Personalize motivation with relevant projects or experiences when useful "
                "and supported. Choose their number and detail freely within the letter's "
                "overall length. No mandatory employer, project, metric or skill citation. "
                "Connect a concrete fact to a role need and explain the contribution it could "
                "support, while keeping motivation central. Do not form a catalogue of "
                "achievements or technical tools."
            ),
            "personalization": (
                "Connect concrete offer details to an expressed interest; repeating the "
                "company name or praising its excellence is not personalization. Check "
                "whether most of the letter could be sent to another organization by changing "
                "only its name; if so, strengthen the connections to documented details of "
                "this opportunity. With sparse company information, focus on the stated "
                "mission or responsibilities without inventing context."
            ),
            "scope": (
                "Subject follows subject instructions; body follows body-only writing rules. "
                "Both use the requested language and remain separate from CV style rules."
            ),
            "language": language,
            "subject": {
                "scope": "motivation_letter_subject (subject in MotivationLetter) only.",
                "candidate_name": profile.identity.full_name,
                "instruction": (
                    "A concise application subject identifying the target role and candidate "
                    "name. An administrative prefix such as Candidature or Application is "
                    "appropriate here. Naming the target role does not claim the candidate "
                    "already holds it. Do not apply body opening or motivation rules here."
                ),
                "target_role": job_title,
            },
        },
        "source_index": sorted(allowed_source_ids(profile, adapted_cv)),
        "candidate_evidence_scope": {
            "source_id": "candidate:provided_evidence",
            "member_source_ids": sorted(_candidate_source_ids(profile, adapted_cv)),
            "scope": (
                "Aggregate index of all candidate declarations and factual records actually "
                "supplied in this request. Its members include identity, declared skills, "
                "the exposed experience/project facts, education, certificates and languages. "
                "This index is not an additional fact or independent proof. It excludes "
                "generated document prose, offer/analysis content and style instructions. "
                "Unselected projects and other unexposed profile records are outside this "
                "scope. An absence finding means unsupported in the supplied evidence, "
                "not false in the real world or absent from the entire candidate profile."
            ),
        },
        "reference_date": {
            "source_id": "context:reference_date",
            "date": (reference_date if reference_date is not None else date.today()).isoformat(),
            "scope": (
                "Reference date for temporal claims. Compare dated source facts with this date; "
                "do not assume a dated position or project is still current. An unspecified "
                "end date alone does not prove current activity."
            ),
        },
        "job_offer": {
            "source_id": "offer:description",
            "title": job_title,
            "company": job_company,
            "full_description": job_description,
        },
        "motivation_context": {
            # An explicit view of the existing analysis, not new evidence or
            # another LLM call. Do not repeat these fields in job_analysis.
            "source_id": "offer:analysis",
            "derived_from": "offer:description",
            "company_context": analysis.company_context,
            "offer_interest_points": list(analysis.offer_interest_points),
            "scope": (
                "Offer-derived leads for why this organization and role could appeal to the "
                "candidate, not independent proof of company facts or the candidate's personal "
                "history/preferences. Check them against the full offer before use. If empty "
                "or contradicted, rely on concrete role details in the offer; do not invent "
                "company values, products, reputation or culture."
            ),
        },
        "job_analysis": {
            "source_id": "offer:analysis",
            "data": analysis.model_dump(
                mode="json",
                exclude={
                    "match_reasons",
                    "risks",
                    "fit_score",
                    "company_context",
                    "offer_interest_points",
                },
            ),
        },
        "selected_project_evidence": projects,
        "final_cv": {
            "source_id": "document:final_cv",
            "read_only": True,
            # Warnings can contain the drafting model's reasoning; the reviewer
            # receives final text and source facts, never that self-assessment.
            "data": adapted_cv.model_dump(
                mode="json",
                exclude={"warnings", "project_selection_reasons", "llm_selected_skills"},
            ),
        },
    }
