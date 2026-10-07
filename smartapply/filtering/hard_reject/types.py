"""Small value objects shared by all hard-rejection gates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class GateStatus(str, Enum):
    """A gate either proves a blocker, clears it, or cannot resolve it."""

    CLEAR = "clear"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


class HardRejectDisposition(str, Enum):
    """The only two outcomes the deterministic rejection stage can produce."""

    REJECTED = "rejected"
    PENDING_RELEVANCE = "pending_relevance"


@dataclass(frozen=True)
class Evidence:
    """Auditable fact used by one deterministic rule."""

    rule_id: str
    source: str
    quote: str
    field: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("rule_id", self.rule_id),
            ("source", self.source),
            ("quote", self.quote),
        ):
            if not value.strip():
                raise ValueError(f"Evidence {name} must not be empty")

    def to_dict(self) -> dict[str, str]:
        payload = {
            "rule_id": self.rule_id,
            "source": self.source,
            "quote": self.quote,
        }
        if self.field:
            payload["field"] = self.field
        return payload


@dataclass(frozen=True)
class GateDecision:
    """Decision and supporting material produced by one independent gate."""

    gate: str
    status: GateStatus
    reason_codes: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    conflicts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is GateStatus.BLOCKED and not self.evidence:
            raise ValueError(f"Blocked gate {self.gate!r} must include evidence")
        if self.status is GateStatus.BLOCKED and self.conflicts:
            raise ValueError(f"Blocked gate {self.gate!r} cannot contain conflicts")

    @property
    def blocked(self) -> bool:
        return self.status is GateStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "evidence": [item.to_dict() for item in self.evidence],
            "conflicts": list(self.conflicts),
            "notes": list(self.notes),
        }

    @classmethod
    def clear(cls, gate: str, *, notes: tuple[str, ...] = ()) -> GateDecision:
        return cls(gate=gate, status=GateStatus.CLEAR, notes=notes)

    @classmethod
    def unresolved(
        cls,
        gate: str,
        *,
        conflicts: tuple[str, ...],
        evidence: tuple[Evidence, ...] = (),
        notes: tuple[str, ...] = (),
    ) -> GateDecision:
        return cls(
            gate=gate,
            status=GateStatus.UNRESOLVED,
            evidence=evidence,
            conflicts=conflicts,
            notes=notes,
        )


@dataclass(frozen=True)
class HardRejectResult:
    """Aggregate result of independent deterministic gates."""

    decisions: tuple[GateDecision, ...]
    context_reasons: tuple[str, ...] = ()
    ruleset_version: str = "hard-reject-v2"

    @property
    def rejected(self) -> bool:
        return any(decision.blocked for decision in self.decisions)

    @property
    def kept(self) -> bool:
        return not self.rejected

    @property
    def disposition(self) -> HardRejectDisposition:
        if self.rejected:
            return HardRejectDisposition.REJECTED
        return HardRejectDisposition.PENDING_RELEVANCE

    @property
    def reasons(self) -> list[str]:
        reasons = list(self.context_reasons)
        for decision in self.decisions:
            reasons.extend(decision.reason_codes)
            reasons.extend(decision.notes)
            reasons.extend(f"{decision.gate}_conflict:{item}" for item in decision.conflicts)
        return reasons

    @property
    def blocking_reasons(self) -> list[str]:
        """Return only reason codes that actually caused the rejection."""

        return [
            reason
            for decision in self.decisions
            if decision.blocked
            for reason in decision.reason_codes
        ]

    def audit_payload(self) -> dict[str, Any]:
        return {
            "ruleset_version": self.ruleset_version,
            "rejected": self.rejected,
            "gates": [decision.to_dict() for decision in self.decisions],
        }

    def with_context_reasons(self, reasons: list[str]) -> HardRejectResult:
        return HardRejectResult(
            decisions=self.decisions,
            context_reasons=tuple(reasons),
            ruleset_version=self.ruleset_version,
        )
