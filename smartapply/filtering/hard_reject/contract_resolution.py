"""Resolve compatible and incompatible contract evidence into one gate result."""

from __future__ import annotations

from dataclasses import dataclass

from smartapply.filtering.hard_reject.contract_evidence import deduplicate_evidence
from smartapply.filtering.hard_reject.types import Evidence, GateDecision, GateStatus

GATE_NAME = "contract"


@dataclass(frozen=True)
class ContractBlocker:
    """One independently supported reason to reject an offer."""

    reason_code: str
    evidence: Evidence
    conflicts_with_permanent_contract: bool = True
    conflicts_with_full_time: bool = False
    conflicts_with_source_uncertainty: bool = False
    overrides_metadata_permanent: bool = False


def decision_from_evidence(
    *,
    blockers: list[ContractBlocker],
    permanent_evidence: tuple[Evidence, ...],
    full_time_evidence: tuple[Evidence, ...],
    source_uncertainty_evidence: tuple[Evidence, ...],
    notes: list[str],
) -> GateDecision:
    """Apply the contract gate's explicit source-conflict policy."""

    conflicting: list[ContractBlocker] = []
    decisive: list[ContractBlocker] = []
    conflict_pairs: list[tuple[ContractBlocker, Evidence]] = []
    for blocker in _deduplicate_blockers(blockers):
        if not (
            blocker.conflicts_with_permanent_contract
            or blocker.conflicts_with_full_time
            or blocker.conflicts_with_source_uncertainty
        ):
            decisive.append(blocker)
            continue

        relevant_compatible: list[Evidence] = []
        if blocker.conflicts_with_permanent_contract:
            relevant_compatible.extend(
                evidence
                for evidence in permanent_evidence
                if not (
                    blocker.overrides_metadata_permanent
                    and evidence.rule_id
                    in {
                        "contract.metadata_permanent",
                        "contract.source_fact_permanent",
                    }
                )
            )
        if blocker.conflicts_with_full_time:
            relevant_compatible.extend(full_time_evidence)
        if blocker.conflicts_with_source_uncertainty:
            relevant_compatible.extend(source_uncertainty_evidence)
        if relevant_compatible:
            conflicting.append(blocker)
            conflict_pairs.extend(
                (blocker, compatible) for compatible in relevant_compatible
            )
        else:
            decisive.append(blocker)

    conflicts = tuple(
        f"{blocker.evidence.source}={blocker.evidence.quote!r} vs "
        f"{compatible.source}={compatible.quote!r}"
        for blocker, compatible in conflict_pairs
    )

    if decisive:
        if conflicting:
            notes.append("contract_conflict_non_decisive")
        return GateDecision(
            gate=GATE_NAME,
            status=GateStatus.BLOCKED,
            reason_codes=tuple(blocker.reason_code for blocker in decisive),
            evidence=tuple(
                deduplicate_evidence([blocker.evidence for blocker in decisive])
            ),
            notes=tuple(dict.fromkeys(notes)),
        )

    if conflicting:
        return GateDecision.unresolved(
            GATE_NAME,
            conflicts=conflicts,
            evidence=tuple(
                deduplicate_evidence(
                    [blocker.evidence for blocker in conflicting]
                    + list(permanent_evidence)
                    + list(full_time_evidence)
                    + list(source_uncertainty_evidence)
                )
            ),
            notes=tuple(dict.fromkeys(notes)),
        )

    return GateDecision(
        gate=GATE_NAME,
        status=GateStatus.CLEAR,
        evidence=tuple(
            deduplicate_evidence(
                list(permanent_evidence) + list(full_time_evidence)
                + list(source_uncertainty_evidence)
            )
        ),
        notes=tuple(dict.fromkeys(notes)),
    )


def _deduplicate_blockers(
    blockers: list[ContractBlocker],
) -> list[ContractBlocker]:
    unique: list[ContractBlocker] = []
    seen: set[tuple[str, str, str]] = set()
    for blocker in blockers:
        key = (
            blocker.reason_code,
            blocker.evidence.source,
            blocker.evidence.quote,
        )
        if key not in seen:
            seen.add(key)
            unique.append(blocker)
    return unique


__all__ = ["ContractBlocker", "decision_from_evidence"]
