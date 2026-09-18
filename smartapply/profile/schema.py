"""Pydantic schemas for the candidate profile.

Each piece of content reusable in a generated CV has a stable ``id`` (the
*source_id*). Experience descriptions are copied verbatim from the profile;
the validator checks their text, completeness and order.

Each ``Bullet`` carries:
- ``evidence_level``: how strong the underlying evidence is when grounding
  generated summaries and letters. It never permits rewriting CV experiences.
- ``allowed_claims``: explicit list of claims the LLM is allowed to make
  about this bullet. Anything outside this list is treated as an invention.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    HttpUrl,
    field_validator,
)

EvidenceLevel = Literal["verified", "self_reported", "inferred"]

# Accept "MM/YYYY", "YYYY-MM", or open-ended markers ("Present", "Current").
_DATE_RE = re.compile(
    r"^(?:(0[1-9]|1[0-2])/\d{4}|\d{4}-(0[1-9]|1[0-2])|Present|Current|Ongoing)$",
    flags=re.IGNORECASE,
)


def _validate_date(value: str) -> str:
    if not _DATE_RE.match(value.strip()):
        raise ValueError(
            f"Date must be MM/YYYY, YYYY-MM, or Present/Current/Ongoing — got {value!r}"
        )
    return value.strip()


class Identity(BaseModel):
    full_name: str = Field(min_length=2)
    title: str
    summary: str
    location: str
    email: EmailStr
    phone: str | None = None
    github: HttpUrl | None = None
    linkedin: HttpUrl | None = None
    website: HttpUrl | None = None


class JobPreferences(BaseModel):
    """What kind of jobs the candidate accepts."""

    target_roles: list[str]
    accepted_contract_types: list[str]
    accepted_remote_policies: list[str]
    accepted_job_languages: list[str] = Field(default_factory=lambda: ["fr", "en"])
    domains_of_interest: list[str] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list)

    @field_validator("accepted_job_languages")
    @classmethod
    def normalize_accepted_job_languages(cls, values: list[str]) -> list[str]:
        """Normalize the French/English spellings accepted in profile JSON."""
        aliases = {
            "fr": "fr",
            "fra": "fr",
            "francais": "fr",
            "français": "fr",
            "french": "fr",
            "frensh": "fr",
            "en": "en",
            "eng": "en",
            "anglais": "en",
            "english": "en",
        }
        normalized: list[str] = []
        for raw in values:
            language = aliases.get(raw.strip().lower(), raw.strip().lower())
            if language and language not in normalized:
                normalized.append(language)
        return normalized


class SkillCategory(BaseModel):
    id: str
    name: str
    skills: list[str]


class SkillProfile(BaseModel):
    """A compact display preset for the CV skills section."""

    id: str
    name: str
    description: str | None = None
    category_skills: dict[str, list[str]]


class Skills(BaseModel):
    categories: list[SkillCategory]
    profiles: list[SkillProfile] = Field(default_factory=list)
    core: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Baseline skills, keyed by category id. They are available as "
            "stable evidence for the LLM, but should only be displayed when "
            "they add value for the target offer."
        ),
    )
    matching_keywords: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Non-display keywords used to detect skill/profile families in "
            "job offers. These terms are not CV skills and must not be shown "
            "unless they also exist in the display skills whitelist."
        ),
    )

    @property
    def allowed_skills(self) -> set[str]:
        """Flat whitelist of canonical display skills.

        ``categories`` is the only source of truth for CV-displayable skills.
        Profiles, core baselines and role contracts may reorder or select from
        this catalog, but they must not make a new display skill claim.
        """
        return {s for c in self.categories for s in c.skills}

    @property
    def profile_ids(self) -> set[str]:
        return {p.id for p in self.profiles}

    def profile_by_id(self, profile_id: str | None) -> SkillProfile | None:
        if not profile_id:
            return None
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    def effective_category_skills(self, profile_id: str | None) -> dict[str, list[str]]:
        """Merge a profile's offer-specific skills with the core baseline.

        Order within each category: profile additions first (most relevant to
        the offer), then core skills. Skills absent from the canonical category
        catalog are ignored.
        """
        profile = self.profile_by_id(profile_id)
        profile_skills: dict[str, list[str]] = (
            dict(profile.category_skills) if profile is not None else {}
        )
        canonical_by_category = {c.id: set(c.skills) for c in self.categories}
        category_ids: list[str] = []
        for cid in list(profile_skills.keys()) + list(self.core.keys()):
            if cid not in category_ids:
                category_ids.append(cid)

        merged: dict[str, list[str]] = {}
        for cid in category_ids:
            ordered: list[str] = []
            seen: set[str] = set()
            for skill in profile_skills.get(cid, []) + self.core.get(cid, []):
                if skill not in canonical_by_category.get(cid, set()):
                    continue
                if skill not in seen:
                    ordered.append(skill)
                    seen.add(skill)
            if ordered:
                merged[cid] = ordered
        return merged


class BulletLink(BaseModel):
    """A hyperlink to wrap around a specific anchor word inside a bullet.

    Renderers scan the bullet's output text for ``anchor`` (case-insensitive,
    first occurrence) and wrap the matched span as a hyperlink to ``url``.
    If the anchor isn't present in the text, the link is dropped silently.
    """

    anchor: str = Field(min_length=1)
    url: HttpUrl


class Bullet(BaseModel):
    """A single bullet of evidence the candidate can claim.

    ``evidence_level`` and ``allowed_claims`` ground generated summaries and
    letters. Experience bullets themselves are inserted verbatim into the CV.
    """

    id: str = Field(min_length=3)
    text: str
    keywords: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    evidence_level: EvidenceLevel = "verified"
    allowed_claims: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit list of claims the LLM is allowed to rephrase. "
            "If empty, the bullet text itself is the only allowed claim."
        ),
    )
    links: list[BulletLink] = Field(
        default_factory=list,
        description=(
            "Hyperlinks to embed in the rendered bullet. Each link names an "
            "anchor word; the renderer wraps the first occurrence of that word "
            "in the adapted bullet as a hyperlink."
        ),
    )

    @property
    def effective_allowed_claims(self) -> list[str]:
        """Falls back to the bullet text when no explicit claims are given."""
        return self.allowed_claims or [self.text]


class Experience(BaseModel):
    id: str
    company: str
    url: HttpUrl | None = None
    title: str
    location: str
    start_date: str
    end_date: str
    bullets: list[Bullet]
    keywords: list[str] = Field(default_factory=list)

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        return _validate_date(value)


class Project(BaseModel):
    """Same anti-hallucination contract as Experience: bullets with stable ids."""

    id: str
    name: str
    bullets: list[Bullet]
    keywords: list[str] = Field(default_factory=list)
    status: str | None = None
    url: HttpUrl | None = None

    @property
    def description(self) -> str:
        """Concatenate project evidence for ranking and document rendering."""
        return " ".join(b.text for b in self.bullets).strip()


class Degree(BaseModel):
    id: str
    title: str
    field: str | None = None
    institution: str
    url: HttpUrl | None = None
    start_date: str | None = None
    end_date: str | None = None
    start_year: int = Field(ge=1900, le=2100)
    end_year: int = Field(ge=1900, le=2100)

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_date(value)

    @field_validator("end_year")
    @classmethod
    def _end_after_start(cls, v: int, info) -> int:
        start = info.data.get("start_year")
        if start is not None and v < start:
            raise ValueError(f"end_year ({v}) must be >= start_year ({start})")
        return v


class Language(BaseModel):
    name: str
    level: str


class Certificate(BaseModel):
    id: str
    name: str
    issuer: str
    description: str | None = None
    date: str | None = None
    url: HttpUrl | None = None


class StyleGuide(BaseModel):
    tone: str
    voice: str
    do: list[str] = Field(default_factory=list)
    dont: list[str] = Field(default_factory=list)
    max_bullet_length: int = Field(default=200, ge=50)
    max_summary_length: int = Field(default=180, ge=80)
    max_summary_lines: int = Field(default=2, ge=1, le=4)


class TemplateStyle(BaseModel):
    """Visual style for the DOCX template — mirrors the candidate's CV."""

    primary_color_hex: str = "3B82F6"
    text_color_hex: str = "1F2937"
    muted_color_hex: str = "6B7280"
    font_family: str = "Calibri"
    title_font_size: int = 24
    section_font_size: int = 12
    body_font_size: int = 10
    left_column_width_cm: float = 4.0


class Profile(BaseModel):
    identity: Identity
    preferences: JobPreferences
    skills: Skills
    experiences: list[Experience]
    projects: list[Project] = Field(default_factory=list)
    education: list[Degree] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    certificates: list[Certificate] = Field(default_factory=list)
    style_guide: StyleGuide
    template_style: TemplateStyle = Field(default_factory=TemplateStyle)

    @field_validator("experiences")
    @classmethod
    def _unique_experience_ids(cls, v: list[Experience]) -> list[Experience]:
        ids = [e.id for e in v]
        if len(set(ids)) != len(ids):
            raise ValueError("Experience ids must be unique")
        return v

    @field_validator("projects")
    @classmethod
    def _unique_project_ids(cls, v: list[Project]) -> list[Project]:
        ids = [p.id for p in v]
        if len(set(ids)) != len(ids):
            raise ValueError("Project ids must be unique")
        return v

    def _all_bullets(self) -> list[tuple[Bullet, Experience | Project]]:
        out: list[tuple[Bullet, Experience | Project]] = []
        for e in self.experiences:
            for b in e.bullets:
                out.append((b, e))
        for p in self.projects:
            for b in p.bullets:
                out.append((b, p))
        return out

    def bullet_index(self) -> dict[str, Bullet]:
        """Bullet id → Bullet, across experiences AND projects."""
        index: dict[str, Bullet] = {}
        for bullet, _ in self._all_bullets():
            if bullet.id in index:
                raise ValueError(f"Duplicate bullet id across profile: {bullet.id}")
            index[bullet.id] = bullet
        return index

    def experience_for_bullet(self, bullet_id: str) -> Experience | None:
        for e in self.experiences:
            for b in e.bullets:
                if b.id == bullet_id:
                    return e
        return None

    def project_for_bullet(self, bullet_id: str) -> Project | None:
        for p in self.projects:
            for b in p.bullets:
                if b.id == bullet_id:
                    return p
        return None

    def block_for_bullet(self, bullet_id: str) -> Experience | Project | None:
        return self.experience_for_bullet(bullet_id) or self.project_for_bullet(bullet_id)

    def model_post_init(self, _context) -> None:  # noqa: D401
        """Cross-field check: no bullet id collisions across experiences AND projects."""
        seen: set[str] = set()
        for bullet, _ in self._all_bullets():
            if bullet.id in seen:
                raise ValueError(f"Duplicate bullet id across profile: {bullet.id}")
            seen.add(bullet.id)
