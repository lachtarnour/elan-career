"""Structural observations for V2; no source grammar or numeric authority."""

from smartapply.llm.fit_context import DECISION_LEVELS, offer_references, resolve
from smartapply.llm.fit_validation import AXES, POSITIVE, PREFERENCE_FIELDS


def inspect_decisions(evaluation, context, offer) -> list[str]:
    candidate = context["candidate_refs"]
    offers = offer_references(offer)
    source = context.get("offer_source", {})
    if source.get("available"):
        offers["offer_source:body"] = {"source_type": "source"}
    observations = []

    def refs(offer_refs, candidate_refs, label):
        if not offer_refs:
            observations.append(f"{label}: at least one offer reference is required")
        for ref in offer_refs:
            if ref not in offers:
                observations.append(f"{label}: unknown offer reference {ref}")
        for ref in candidate_refs:
            if ref not in candidate:
                observations.append(f"{label}: unknown candidate reference {ref}")

    def binding_preferences(references):
        return bool(references) and all(
            ref in candidate
            and candidate[ref]["source_type"] == "preference"
            and candidate[ref]["path"][1] in PREFERENCE_FIELDS
            for ref in references
        )

    optional = set()
    for interpretation in evaluation.offer_interpretations:
        refs(interpretation.offer_refs, [], "source interpretation")
        valid_quote = (
            source.get("available")
            and bool(interpretation.source_quote.strip())
            and interpretation.source_quote in source.get("offer_body", "")
        )
        if not valid_quote:
            observations.append("Source interpretation: quote not found in frozen source")
        elif interpretation.meaning == "optional":
            optional.update(interpretation.offer_refs)
        if not interpretation.text.strip():
            observations.append("Source interpretation: explanation is empty")

    whole_neutral = set()
    for neutral in evaluation.neutral_offer_refs:
        refs([neutral.offer_ref], neutral.candidate_refs, "neutralisation")
        kind = offers.get(neutral.offer_ref, {}).get("source_type")
        if neutral.scope == "years_component" and neutral.reason != "years_only":
            observations.append(f"{neutral.offer_ref}: years_component requires years_only")
        if neutral.reason == "compatible_preference" and not binding_preferences(
            neutral.candidate_refs
        ):
            observations.append(
                f"{neutral.offer_ref}: compatible_preference requires admissible preference evidence"
            )
        if (
            neutral.reason in {"no_declared_preference", "compatible_preference"}
            and kind != "constraints"
        ):
            observations.append(
                f"{neutral.offer_ref}: preference neutralisation requires a context constraint"
            )
        if neutral.scope == "whole_entry":
            admissible = neutral.reason in {"years_only", "explicit_exemption"}
            admissible |= neutral.reason == "optional_only" and neutral.offer_ref in optional
            admissible |= kind == "constraints" and neutral.reason in {
                "no_declared_preference",
                "compatible_preference",
            }
            if admissible:
                whole_neutral.add(neutral.offer_ref)
            else:
                observations.append(
                    f"{neutral.offer_ref}: whole neutralisation lacks appropriate support"
                )

    covered = {}
    for axis in AXES:
        findings = getattr(evaluation.dimensions, axis).findings
        covered[axis] = set()
        positives = 0
        for finding in findings:
            refs(finding.offer_refs, finding.candidate_refs, axis)
            covered[axis].update(finding.offer_refs)
            if not finding.text.strip():
                observations.append(f"{axis}: finding explanation is empty")
            sources = [candidate[ref] for ref in finding.candidate_refs if ref in candidate]
            types = {item["source_type"] for item in sources}
            if finding.support in POSITIVE:
                positives += 1
                if not sources or types & {"identity", "preference"}:
                    observations.append(
                        f"{axis}: positive capability needs appropriate candidate facts"
                    )
                if axis != "essentials" and "bullet" not in types:
                    observations.append(
                        f"{axis}: accomplishments require experience/project bullets"
                    )
                if finding.support in {"direct", "transferable"} and types == {"declared_skill"}:
                    observations.append(
                        f"{axis}: ordinary declared skills must remain declared_only"
                    )
                if finding.support == "direct" and any(
                    item["source_type"] == "bullet"
                    and resolve(context["candidate_facts"], item)["evidence_level"] == "inferred"
                    for item in sources
                ):
                    observations.append(f"{axis}: inferred facts are not established realizations")
            if finding.support == "not_documented" and sources:
                observations.append(
                    f"{axis}: not_documented must distinguish positive partial support"
                )
            if finding.support == "contradicted" and (
                not sources or types <= {"declared_skill", "identity"}
            ):
                observations.append(f"{axis}: contradiction needs an explicit candidate fact")
        state = getattr(evaluation.decision, axis)
        if DECISION_LEVELS[state] >= 3 and not positives:
            observations.append(f"{axis}: high decision without a sourced positive finding")
        if (DECISION_LEVELS[state] <= 2 or state == "credible_material_question") and not any(
            issue.axis == axis for issue in evaluation.issues
        ):
            observations.append(f"{axis}: material decision needs an explanatory reservation")

    issue_coverage = set()
    task_coverage = set()
    for issue in evaluation.issues:
        refs(issue.offer_refs, issue.candidate_refs, issue.axis)
        issue_coverage.update(issue.offer_refs)
        if issue.origin == "offer_information" and issue.axis in {"missions", "responsibilities"}:
            task_coverage.update(issue.offer_refs)
        if not issue.text.strip():
            observations.append(f"{issue.axis}: reservation explanation is empty")
        if issue.origin == "preference_alignment" and not any(
            ref in candidate and candidate[ref]["source_type"] == "preference"
            for ref in issue.candidate_refs
        ):
            observations.append("Preference reservation needs a declared preference")
    if evaluation.decision.preference_conflict == "confirmed" and not any(
        issue.axis == "context"
        and issue.origin == "preference_alignment"
        and binding_preferences(issue.candidate_refs)
        and any(
            ref in offers and offers[ref]["source_type"] in {"constraints", "source"}
            for ref in issue.offer_refs
        )
        for issue in evaluation.issues
    ):
        observations.append(
            "Confirmed preference conflict needs binding preference and offer condition"
        )
    for ref, entry in offers.items():
        if ref in whole_neutral or ref in optional:
            continue
        kind = entry["source_type"]
        if kind == "required_skills" and ref not in covered["essentials"]:
            observations.append(f"{ref}: essential expectation not covered")
        if kind == "main_tasks" and ref not in (
            covered["missions"] | covered["responsibilities"] | task_coverage
        ):
            observations.append(f"{ref}: task not covered")
        if kind == "constraints" and ref not in (set.union(*covered.values()) | issue_coverage):
            observations.append(
                f"{ref}: constraint not covered (partial neutralisation is insufficient)"
            )
    return list(dict.fromkeys(observations))
