"""Shared immutable input for deterministic rejection gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from smartapply.company_identity import source_company_of
from smartapply.filtering.rules import RuleSet
from smartapply.filtering.source_facts import FilterFacts
from smartapply.filtering.text import norm
from smartapply.filtering.types import HasJobFields

if TYPE_CHECKING:
    from smartapply.profile import Profile


@dataclass(frozen=True)
class CandidateQualifications:
    """Profile facts that can safely participate in hard requirements."""

    has_doctorate: bool | None = None

    @classmethod
    def from_profile(cls, profile: Profile) -> CandidateQualifications:
        if not profile.education:
            return cls(has_doctorate=None)
        degree_text = "\n".join(
            filter(
                None,
                (
                    f"{degree.title} {degree.field or ''}"
                    for degree in profile.education
                ),
            )
        )
        normalized = norm(degree_text)
        doctorate_markers = (
            "doctorat",
            "doctorate",
            "doctoral",
            "phd",
            "ph d",
        )
        return cls(has_doctorate=any(marker in normalized for marker in doctorate_markers))


@dataclass(frozen=True)
class HardRejectContext:
    """All normalized and source-backed data required by a rejection gate."""

    job: HasJobFields
    rules: RuleSet
    facts: FilterFacts
    qualifications: CandidateQualifications
    title: str
    company: str
    description: str
    contract: str
    remote: str

    @property
    def combined(self) -> str:
        return f"{self.title}\n{self.description}"

    @classmethod
    def build(
        cls,
        *,
        job: HasJobFields,
        rules: RuleSet,
        facts: FilterFacts,
        qualifications: CandidateQualifications | None = None,
    ) -> HardRejectContext:
        return cls(
            job=job,
            rules=rules,
            facts=facts,
            qualifications=qualifications or CandidateQualifications(),
            title=norm(job.title),
            company=norm(source_company_of(job)),
            description=norm(job.description),
            contract=norm(job.contract_type),
            remote=norm(job.remote_policy or facts.structured_remote_policy),
        )
