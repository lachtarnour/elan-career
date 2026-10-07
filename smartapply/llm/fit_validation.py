"""Mechanical checks for evidence diagnostics; not a semantic entailment judge."""

from __future__ import annotations

import re

from smartapply.llm.fit_context import offer_references, resolve
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.schemas import JobFitEvaluation, OfferAnalysis

AXES = ("missions", "essentials", "responsibilities")
POSITIVE = {"direct", "transferable", "declared_only"}
SUBSTANTIAL = {"distant_core_work", "substantial_core_gap", "unclear_core_work"}
MATERIAL = SUBSTANTIAL | {"material_core_question"}
PREFERENCE_FIELDS = {"accepted_contract_types", "accepted_remote_policies", "deal_breakers"}
# This narrow grammar recognizes only pure tenure phrases, not occupational fit.
# More complex requirements retain their original entry and require coverage.
_NUMBER = r"(?:\d+(?:[.,]\d+)?|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|one|two|three|four|five|seven|eight|nine|ten)"
_PERIOD = rf"{_NUMBER}(?:\s*[-–à]\s*{_NUMBER}|\s+to\s+{_NUMBER})?\s*\+?\s*(?:ans?|années?|years?)"
_EXPERIENCE = r"(?:\s*['’]?\s+(?:of\s+)?(?:professional\s+)?experience|\s+d['’]expérience(?:\s+professionnelle)?)"
_TENURE = re.compile(
    rf"(?:at least |minimum |au moins |minimum de |plus de )?{_PERIOD}(?:{_EXPERIENCE})?"
    r"(?:\s+(?:required|requested|demandés?|requise?s?|minimum))?[. ]*",
    re.I,
)

_TENURE_COMPONENT = re.compile(
    rf"(?:{_PERIOD}{_EXPERIENCE}|^\s*{_PERIOD}(?=\s+(?:et|and|en|in|avec|with)\s+))",
    re.I,
)


def pure_tenure(text: str) -> bool:
    return _TENURE.fullmatch(text.strip()) is not None


def validate_evaluation(
    evaluation: JobFitEvaluation, context: dict, offer: OfferAnalysis
) -> JobFitEvaluation:
    # Revalidate even a provider's model_construct object; bool is not an integer level.
    evaluation = JobFitEvaluation.model_validate(evaluation.model_dump())
    candidate = context["candidate_refs"]
    offers = offer_references(offer)
    facts = context["candidate_facts"]
    errors = []

    def fail(message):
        errors.append(message)

    def check_refs(offer_refs, candidate_refs, label):
        if not offer_refs:
            fail(f"{label}: at least one offer reference is required")
        for ref in offer_refs:
            if ref not in offers:
                fail(f"{label}: unknown offer reference {ref}")
        for ref in candidate_refs:
            if ref not in candidate:
                fail(f"{label}: unknown candidate reference {ref}")

    def preference_refs(refs):
        return bool(refs) and all(
            ref in candidate
            and candidate[ref]["source_type"] == "preference"
            and candidate[ref]["path"][1] in PREFERENCE_FIELDS
            for ref in refs
        )

    whole_neutral = set()
    for neutral in evaluation.neutral_offer_refs:
        ref = neutral.offer_ref
        check_refs([ref], neutral.candidate_refs, "neutralisation")
        if ref not in offers:
            continue
        kind = offers[ref]["source_type"]
        text = resolve(offer.model_dump(), offers[ref])
        if neutral.scope == "years_component" and neutral.reason != "years_only":
            fail(f"{ref}: years_component requires years_only")
        if neutral.reason == "years_only":
            if neutral.scope == "whole_entry" and not pure_tenure(text):
                fail(f"{ref}: not a mechanically recognizable pure professional tenure entry")
            if neutral.scope == "years_component" and (
                pure_tenure(text) or not _TENURE_COMPONENT.search(text)
            ):
                fail(
                    f"{ref}: years_component needs explicit professional tenure plus remaining capability"
                )
            if kind not in {"constraints", "required_skills", "main_tasks"}:
                fail(f"{ref}: tenure neutralisation has inappropriate source")
        if (
            kind in {"required_skills", "main_tasks"}
            and neutral.scope == "whole_entry"
            and neutral.reason not in {"years_only", "explicit_exemption"}
        ):
            fail(f"{ref}: mandatory work cannot be neutralized by this reason")
        if (
            neutral.reason in {"no_declared_preference", "compatible_preference"}
            and kind != "constraints"
        ):
            fail(f"{ref}: preference neutralisation requires a context constraint")
        if neutral.reason == "compatible_preference" and not preference_refs(
            neutral.candidate_refs
        ):
            fail(f"{ref}: compatible_preference requires admissible preference evidence")
        if neutral.scope == "whole_entry":
            whole_neutral.add(ref)
    findings_by_axis = {}
    for axis in AXES:
        dimension = getattr(evaluation.dimensions, axis)
        findings_by_axis[axis] = set()
        positives = 0
        for finding in dimension.findings:
            if not finding.text.strip():
                fail(f"{axis}: finding explanation is empty")
            check_refs(finding.offer_refs, finding.candidate_refs, axis)
            findings_by_axis[axis].update(finding.offer_refs)
            sources = [candidate[ref] for ref in finding.candidate_refs if ref in candidate]
            types = {source["source_type"] for source in sources}
            if finding.support in POSITIVE:
                if not sources or "identity" in types or "preference" in types:
                    fail(f"{axis}: positive capability needs appropriate candidate facts")
                if axis != "essentials" and types - {"bullet"}:
                    fail(f"{axis}: accomplishments require experience/project bullets")
                if finding.support == "declared_only" and types - {
                    "declared_skill",
                    "languages",
                    "education",
                    "certificates",
                }:
                    fail(f"{axis}: declared_only is not a realization")
                if finding.support in {"direct", "transferable"} and "declared_skill" in types:
                    fail(f"{axis}: ordinary declared skills must remain declared_only")
                if finding.support == "direct" and any(
                    source["source_type"] == "bullet"
                    and resolve(facts, source)["evidence_level"] == "inferred"
                    for source in sources
                ):
                    fail(f"{axis}: inferred facts are not established realizations")
                if (
                    dimension.level == 4
                    and sources
                    and all(
                        source["source_type"] == "bullet"
                        and resolve(facts, source)["evidence_level"] == "inferred"
                        for source in sources
                    )
                ):
                    fail(f"{axis}: only inferred evidence cannot establish level 4")
                allowed = {
                    "missions": {"main_tasks", "supporting_excerpts"},
                    "essentials": {
                        "required_skills",
                        "main_tasks",
                        "constraints",
                        "supporting_excerpts",
                    },
                    "responsibilities": {
                        "main_tasks",
                        "constraints",
                        "seniority",
                        "supporting_excerpts",
                    },
                }[axis]
                if not any(
                    ref in offers and offers[ref]["source_type"] in allowed
                    for ref in finding.offer_refs
                ):
                    fail(f"{axis}: positive finding has no expectation relevant to this axis")
                positives += 1
            if finding.support == "contradicted" and (
                not sources
                or "identity" in types
                or types <= {"declared_skill"}
                or any(
                    source["source_type"] == "bullet"
                    and resolve(facts, source)["evidence_level"] == "inferred"
                    for source in sources
                )
            ):
                fail(f"{axis}: contradiction needs an explicit candidate fact")
            if finding.support == "not_documented" and sources:
                fail(f"{axis}: missing evidence must not cite a skill as inability")
        if dimension.level >= 3 and not positives:
            fail(f"{axis}: high level without a sourced positive finding")
    issue_coverage = set()
    task_issue_coverage = set()
    for issue in evaluation.issues:
        if not issue.text.strip():
            fail(f"{issue.code}: issue explanation is empty")
        check_refs(issue.offer_refs, issue.candidate_refs, issue.code)
        issue_coverage.update(issue.offer_refs)
        if (
            issue.code in {"unclear_core_work", "substantial_core_gap"}
            and issue.origin == "offer_information"
        ):
            task_issue_coverage.update(issue.offer_refs)
        kinds = {offers[ref]["source_type"] for ref in issue.offer_refs if ref in offers}
        if issue.code in MATERIAL | {"confirmed_constraint_conflict"} and (
            kinds <= {"nice_to_have"} or set(issue.offer_refs) <= whole_neutral
        ):
            fail(f"{issue.code}: material problem cannot rely solely on optional/neutral facts")
        if issue.code == "confirmed_constraint_conflict":
            if issue.axis != "context" or issue.origin != "preference_alignment":
                fail("confirmed_constraint_conflict: requires context/preference_alignment")
            if not preference_refs(issue.candidate_refs) or not kinds & {"constraints"}:
                fail(
                    "confirmed_constraint_conflict: requires binding preference and offer condition"
                )
        if issue.code == "offer_clarity" and (
            issue.axis != "context" or issue.origin != "offer_information"
        ):
            fail("offer_clarity: requires context/offer_information")
        if issue.code in MATERIAL:
            if issue.axis not in AXES:
                fail(f"{issue.code}: requires a technical dimension")
                continue
            level = getattr(evaluation.dimensions, issue.axis).level
            maximum = {
                "distant_core_work": 1,
                "substantial_core_gap": 2,
                "unclear_core_work": 2,
                "material_core_question": 3,
            }[issue.code]
            if level > maximum:
                fail(f"{issue.code}: incoherent {issue.axis} level {level}")
        if issue.code in {"distant_core_work", "unclear_core_work"} and issue.axis != "missions":
            fail(f"{issue.code}: requires missions axis")
        if issue.code == "distant_core_work" and not kinds & {"main_tasks"}:
            fail("distant_core_work: must cite actual activities")
        if issue.code == "distant_core_work" and issue.origin == "offer_information":
            fail("distant_core_work: unknown offer information is not distance")
        if issue.code == "unclear_core_work" and issue.origin != "offer_information":
            fail("unclear_core_work: requires offer_information")
        if issue.origin == "preference_alignment" and not any(
            ref in candidate and candidate[ref]["source_type"] == "preference"
            for ref in issue.candidate_refs
        ):
            fail(f"{issue.code}: preference alignment needs a declared preference")
    for axis in AXES:
        if getattr(evaluation.dimensions, axis).level <= 2 and not any(
            issue.axis == axis and issue.code in SUBSTANTIAL for issue in evaluation.issues
        ):
            fail(f"{axis}: partial/weak level requires a substantial explanatory issue")
    for ref, entry in offers.items():
        if ref in whole_neutral:
            continue
        kind = entry["source_type"]
        if kind == "required_skills" and ref not in findings_by_axis["essentials"]:
            fail(f"{ref}: essential requirement not covered")
        if kind == "main_tasks" and ref not in (
            findings_by_axis["missions"]
            | findings_by_axis["responsibilities"]
            | task_issue_coverage
        ):
            fail(f"{ref}: task not covered")
        if kind == "constraints" and ref not in (
            set.union(*findings_by_axis.values()) | issue_coverage
        ):
            fail(f"{ref}: constraint not covered (partial neutralisation is insufficient)")
    if errors:
        raise LLMValidationError("Invalid fit diagnostic: " + "; ".join(dict.fromkeys(errors)))
    return evaluation
