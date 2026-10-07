"""Shared types for the local filtering pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Protocol


class FilterDisposition(str, Enum):
    """The two outcomes of deterministic rejection, before relevance analysis."""

    PENDING_RELEVANCE = "pending_relevance"
    REJECTED = "rejected"


class HasJobFields(Protocol):
    title: str
    company: str
    description: str
    location: str | None
    contract_type: str | None
    remote_policy: str | None
