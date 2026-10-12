"""Pydantic schemas for LLM outputs and assembled application documents.

LLM response models define exactly what the model may produce. AdaptedCV
is the final assembled snapshot, including static profile experiences.
Keep response models small, flat and JSON-Schema friendly for strict mode.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_strict = ConfigDict(extra="forbid")


class OfferAnalysis(BaseModel):
    """Offer facts extracted without access to a candidate profile."""

    model_config = _strict

    role_type: str = Field(description="Concise role label, e.g. 'Data Scientist NLP'")
    seniority: str = Field(
        description="Seniority supported by the offer: junior, mid, senior, lead, manager; empty if unclear"
    )
    domain: str = Field(description="Industry / vertical the role sits in")
    main_tasks: list[str] = Field(description="3-7 concrete responsibilities, short bullets")
    required_skills: list[str] = Field(
        description=(
            "Hard skills explicitly requested or clearly listed in the offer body only. "
            "Do not add candidate-profile skills, broad domains, generic soft skills, "
            "or inferred keywords from the title."
        )
    )
    nice_to_have: list[str] = Field(description="Bonus skills appreciated but not required")
    cv_keywords_to_include: list[str] = Field(
        description=(
            "Offer-grounded CV keywords visible in or strongly supported by the offer body. "
            "Return a short list when few reliable keywords exist; do not add candidate-only "
            "skills, generic filler, or title-only inferences."
        )
    )
    offer_language: str = Field(
        default="fr",
        description=(
            "ISO 639-1 code of the language the offer is written in "
            "('fr', 'en', 'de', 'es', ...). Drives the motivation-letter language."
        ),
    )
    extracted_company_name: str = Field(
        default="",
        description=(
            "If the structured company field is missing, generic, or names a "
            "job board / recruitment intermediary instead of the real employer "
            "(e.g. 'Entreprise non communiquée', 'Confidentiel', 'Forums "
            "Talents Handicap', empty), extract the real hiring company name "
            "from the description text. Look for "
            'patterns like "L\'entreprise X recherche", "Notre client X", '
            '"Rejoignez X", "Nous sommes X", or a \'company-name - tagline\' '
            "header. Return only the company's name, no description. "
            "Return an empty string when no real name can be confidently "
            "extracted — never invent."
        ),
    )
    extracted_location: str = Field(
        default="",
        description=(
            "Most specific job location explicitly visible in the offer body, "
            "for example 'Paris', 'Massy', 'Remote (France)' or "
            "'Paris / Lyon'. Do not copy the structured scraper location unless "
            "the body confirms it. Return an empty string when the body gives no "
            "reliable location. Never infer or use external knowledge."
        ),
    )
    company_context: str = Field(
        default="",
        description=(
            "One concise, offer-body-grounded sentence about the company, product, "
            "sector, clients, team, culture or business context. Do not infer from "
            "company name, URL host or outside knowledge. Empty string when the body "
            "gives no reliable context."
        ),
    )
    offer_interest_points: list[str] = Field(
        default_factory=list,
        description=(
            "0-5 concrete reasons a candidate could be interested in this specific "
            "offer, extracted only from the offer body: company/about-us facts, "
            "product, mission, sector, context, stakes, team, clients, culture or "
            "responsibilities. Avoid generic filler and candidate claims. Empty list "
            "when only generic information is available."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit offer constraints such as years of experience, education, contract, "
            "working location, remote policy, languages or work authorization; "
            "preserve mandatory versus preferred wording. Never infer candidate compatibility."
        ),
    )
    supporting_excerpts: list[str] = Field(
        default_factory=list,
        description=(
            "Short exact quotes from the offer body supporting its main missions, "
            "essential requirements, seniority and constraints. No invented or paraphrased quotes."
        ),
    )
    offer_risks: list[str] = Field(
        default_factory=list,
        description=(
            "Uncertainties in the offer itself: missing detail, conflicting requirements, "
            "unclear employer or location. No candidate-specific gaps or fit judgement."
        ),
    )


ExtractionSeniority = Literal["junior", "mid", "senior", "lead", "manager", "mixed", "unknown"]
ExtractionLanguage = Literal["fr", "en", "mixed_fr_en", "other"]
MissionRelation = Literal["direct", "adjacent", "clearly_outside", "unknown"]
MissionEvidence = Literal["sufficient", "insufficient", "contradictory"]


class OfferExtractionOutput(OfferAnalysis):
    """Closed categories for new extractions; historical offer facts stay readable."""

    seniority: ExtractionSeniority = Field(
        description=(
            "Seniority explicitly supported by the offer: junior, mid, senior, lead, manager; "
            "mixed for multiple stated levels, unknown when not established."
        )
    )
    offer_language: ExtractionLanguage = Field(
        default="other",
        description=(
            "Language the offer is written in: fr for French, en for English, "
            "mixed_fr_en for substantive content in both French and English; "
            "other for any other language, other multilingual content, or unclear language. "
            "Required candidate languages do not determine the offer language."
        ),
    )


class MissionScreen(BaseModel):
    """Offer-only mission classification for the dedicated prefilter."""

    model_config = _strict

    mission_relation: MissionRelation = Field(
        description=(
            "Semantic relation of concrete responsibilities to AI/ML, data analysis, "
            "quantitative analysis/research, analytics, "
            "data/AI consulting or closely related data/AI delivery. Exact search terms "
            "are not required; recognise synonymous or functionally equivalent duties. "
            "Financial/economic work requires actual quantitative research or analysis; "
            "general financial duties alone do not qualify, and sectors are not excluded wholesale. "
            "Programming is not required. Generic software/IT work without substantive data/AI duties, "
            "transferable skills or incidental tools do not establish proximity."
        )
    )
    mission_evidence: MissionEvidence = Field(
        description=(
            "Whether the body establishes the actual duties clearly enough to classify their "
            "relation; contradictory means conflicting missions prevent classification."
        )
    )


class JobScopeScreening(BaseModel):
    """Independent mission screen using the unchanged historical categories."""

    model_config = _strict
    mission_screen: MissionScreen = Field(
        description=(
            "Classify the offer's main duties independently of any candidate. "
            "Use unknown with insufficient or contradictory evidence when the duties cannot be classified."
        ),
    )


class FitFinding(BaseModel):
    model_config = _strict
    offer_refs: list[str]
    candidate_refs: list[str]
    support: Literal["direct", "transferable", "declared_only", "not_documented", "contradicted"]
    text: str = Field(min_length=1)


class NeutralOfferReference(BaseModel):
    model_config = _strict
    offer_ref: str
    reason: Literal[
        "years_only",
        "explicit_exemption",
        "optional_only",
        "no_declared_preference",
        "compatible_preference",
    ]
    scope: Literal["whole_entry", "years_component"]
    candidate_refs: list[str]


FitDecisionState = Literal[
    "very_low_support",
    "weak_support",
    "partial_core_fit",
    "credible_fit",
    "strong_fit",
]


class FitDecisions(BaseModel):
    model_config = _strict
    missions: FitDecisionState
    essentials: FitDecisionState
    responsibilities: FitDecisionState


class FitDecisionFinding(FitFinding):
    support: Literal[
        "direct",
        "transferable",
        "declared_only",
        "partial_support",
        "not_documented",
        "contradicted",
    ]


class FitDecisionDimension(BaseModel):
    model_config = _strict
    findings: list[FitDecisionFinding]


class FitDecisionAnnotations(BaseModel):
    model_config = _strict
    missions: FitDecisionDimension
    essentials: FitDecisionDimension
    responsibilities: FitDecisionDimension


class FitReservation(BaseModel):
    model_config = _strict
    axis: Literal["missions", "essentials", "responsibilities", "context"]
    origin: Literal["candidate_evidence", "offer_information", "preference_alignment"]
    offer_refs: list[str]
    candidate_refs: list[str]
    text: str


class FitOfferInterpretation(BaseModel):
    model_config = _strict
    offer_refs: list[str]
    source_ref: Literal["offer_source:body"]
    source_quote: str
    meaning: Literal["mandatory", "optional", "alternative", "conditional", "uncertain"]
    origin: Literal["offer_information"]
    text: str


class FitReliability(BaseModel):
    model_config = _strict
    offer_information: str
    candidate_information: str
    traceability: str
    business_uncertainties: list[str]


class JobFitDecisionEvaluation(BaseModel):
    """Three technical decisions suffice; auxiliary output is advisory at runtime."""

    model_config = _strict
    decision: FitDecisions
    reliability: FitReliability | None = None
    dimensions: FitDecisionAnnotations
    issues: list[FitReservation]
    neutral_offer_refs: list[NeutralOfferReference]
    offer_interpretations: list[FitOfferInterpretation]


class JobFitAssessment(BaseModel):
    """The matching call cannot return or overwrite extracted offer fields."""

    model_config = _strict

    fit_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Ordinal technical fit index from the three weighted decisions, not a probability.",
    )
    match_reasons: list[str] = Field(
        description="Concrete matches between immutable offer requirements and candidate facts."
    )
    risks: list[str] = Field(
        description=(
            "Candidate-specific gaps or uncertainties; distinguish a documented mismatch "
            "from a skill or experience that is simply not documented."
        )
    )


class JobAnalysis(OfferAnalysis, JobFitAssessment):
    """Compatible assembled analysis, including historical single-call records."""

    # Historical callers sometimes omit the score when reconstructing a document
    # context. New fit responses use JobFitAssessment, where the score is required.
    fit_score: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Overall fit of the offer for the candidate, between 0 and 1.",
    )


class AdaptedBullet(BaseModel):
    model_config = _strict

    source_id: str = Field(
        description=(
            "ID of the source profile bullet (must exactly match a bullet id "
            "from the candidate profile). REQUIRED — used by validator."
        )
    )
    text: str = Field(description="Profile bullet text copied verbatim during CV assembly")


class AdaptedExperience(BaseModel):
    model_config = _strict

    source_id: str = Field(description="ID of the source profile experience")
    bullets: list[AdaptedBullet]


class SkillSelectionBlock(BaseModel):
    model_config = _strict

    category_id: str = Field(description="Profile skill category id, e.g. ml_ai or data_infra")
    skills: list[str] = Field(description="Exact display skills selected from allowed_skills")


class ProjectSelectionReason(BaseModel):
    """Internal selection audit, never recruiter-facing evidence."""

    model_config = _strict

    project_id: str
    reason: str = Field(
        description="Concrete direct relevance or honest transferable strengths for the offer"
    )
    offer_evidence: str = Field(
        description="Exact excerpt from offer main_tasks, required_skills or full description"
    )
    complementary_value: str = Field(
        description="Distinct contribution of a specialized project; may be empty for primary"
    )


class CVSelection(BaseModel):
    """First-call output: select the CV content before writing its summary."""

    model_config = _strict

    # Freeze the selected content before the second call writes any summary.
    selected_project_ids: list[str] = Field(
        description=(
            "Exactly 3 unique profile project IDs in display order for new generation. "
            "Choose the strongest direct matches, then primary projects with the best "
            "transferable strengths. At most one specialized project with explicit offer "
            "evidence and complementary value. Never select disabled projects or invent fit."
        )
    )
    selected_certificate_ids: list[str] = Field(
        description="Zero to two unique profile certificate IDs, in CV display order. Select only certificates relevant to the offer."
    )
    project_selection_reasons: list[ProjectSelectionReason] = Field(
        default_factory=list,
        description="Exactly one internal justification per selected project",
    )
    cv_title: str = Field(description="Headline shown under the candidate name")
    skills_profile_id: str = Field(
        default="",
        description=(
            "ID of the skill display profile to use in the CV skills section, "
            "e.g. mixed, llm, machine_learning, reinforcement_learning, data_analyst."
        ),
    )
    selected_skills: list[SkillSelectionBlock] = Field(
        default_factory=list,
        description=(
            "Exact skills to display in the CV, grouped by profile skill category id. "
            "Each skill must come from allowed_skills. Include every offer-required "
            "skill that exists in the candidate whitelist."
        ),
    )
    skills_order: list[str] = Field(
        description="Ordered list of profile skill category ids — most relevant first"
    )
    warnings: list[str] = Field(
        description="Any concern the model wants to surface to the validator"
    )


class CVPersonalization(CVSelection):
    """Complete personalization retained for existing CV snapshots."""

    # None marks a snapshot created before certificate selection existed.
    selected_certificate_ids: list[str] | None = None

    professional_summary: str = Field(
        description=(
            "Offer-adapted professional summary, max 2 lines and grounded only "
            "in the candidate profile."
        )
    )


class AssembledCV(CVSelection):
    """Validated CV content, without a provisional professional summary."""

    selected_experiences: list[AdaptedExperience] = Field(
        description="Complete profile experiences copied verbatim in profile order"
    )
    llm_selected_skills: list[SkillSelectionBlock] | None = Field(
        default=None,
        description="Internal original skill ordering captured before deterministic additions",
    )


class AdaptedCV(CVPersonalization):
    """Complete CV snapshot used by validation, persistence and rendering.

    Never use this model as an LLM response schema. Experience descriptions
    are supplied by the application from the approved profile.
    """

    selected_experiences: list[AdaptedExperience] = Field(
        description="Complete profile experiences copied verbatim in profile order"
    )
    llm_selected_skills: list[SkillSelectionBlock] | None = Field(
        default=None,
        description="Internal original skill ordering captured before deterministic additions",
    )


class MotivationLetter(BaseModel):
    model_config = _strict

    subject: str = Field(description="Motivation letter subject line")
    body: str = Field(
        description=(
            "180-240 words in three paragraphs, centered on why the candidate wants "
            "to join this organization and work on this role's priorities. Open with a "
            "specific reason for applying, using short, direct, professional sentences. "
            "Connect motivation and prospective contribution using reliable offer context "
            "and relevant projects or experiences "
            "when useful, with freely chosen examples and detail within the overall length, "
            "never a CV or skills recap. Company facts follow the offer; "
            "any past realization follows read-only experience or selected-project evidence."
        )
    )


class ApplicationTexts(BaseModel):
    """Second-call output; all selected CV content remains read-only."""

    model_config = _strict

    professional_summary: str = Field(
        description=CVPersonalization.model_fields["professional_summary"].description
    )
    motivation_letter_subject: str = Field(
        description=MotivationLetter.model_fields["subject"].description
    )
    motivation_letter_body: str = Field(
        description=MotivationLetter.model_fields["body"].description
    )

    def to_motivation_letter(self) -> MotivationLetter:
        return MotivationLetter(
            subject=self.motivation_letter_subject,
            body=self.motivation_letter_body,
        )


class MotivationLetterRepair(BaseModel):
    """Targeted replacement for a motivation-letter body."""

    model_config = _strict

    body: str = Field(
        description=(
            "Repaired motivation-letter body only. Preserve the original language "
            "and facts while correcting the single requested defect."
        )
    )


class DocumentReviewIssue(BaseModel):
    """A grounded finding, optionally paired with one exact text replacement."""

    model_config = _strict

    field: Literal[
        "cv_title", "professional_summary", "motivation_letter_subject", "motivation_letter_body"
    ] = Field(description="The existing text field containing the problem")
    category: Literal["factual", "language", "structure", "style"] = Field(
        description=(
            "factual: unsupported attribution or source contradiction; language: concrete "
            "grammar or requested-language defect. These block approval. structure and "
            "style findings are reported separately; objective structure constraints are "
            "also checked by the application. Style findings never authorize an automatic "
            "replacement; never classify a stylistic preference as factual."
        )
    )
    passage: str = Field(
        description="Exact, non-empty quote; an edited passage must occur only once in its field"
    )
    reason: str = Field(
        description="Concrete explanation of the problem and its relation to cited sources"
    )
    source_ids: list[str] = Field(
        description=(
            "Non-empty IDs from source_index identifying the factual records or scope "
            "audited. Use specific relevant records when available; candidate:provided_evidence "
            "covers an unsupported candidate claim with no relevant specific source. "
            "A scope citation is not proof of the disputed claim or its real-world falsity."
        )
    )
    replacement: str | None = Field(
        description=(
            "Minimal replacement for this passage only, empty string to delete it, or null "
            "when no correction is proposed. Must be null for category=style and when "
            "corrections are disabled. "
            "Preserve all text outside the passage; edits must not overlap."
        )
    )


class DocumentTextReview(BaseModel):
    """Only findings and targeted edits; no full-document rewrite or CV selection edits."""

    model_config = _strict

    issues: list[DocumentReviewIssue] = Field(
        description=(
            "Demonstrated defects only, each with its scope and optional targeted correction. "
            "Empty when none remain. Never include positive assessments or a clean verdict."
        )
    )
