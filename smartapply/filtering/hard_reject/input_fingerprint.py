"""Content identity for deterministic hard-rejection decisions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from smartapply.company_identity import source_company_of
from smartapply.filtering.types import HasJobFields


def hard_reject_input_payload(job: HasJobFields) -> dict[str, Any]:
    """Return every persisted input that can affect a hard-reject decision."""

    return {
        "title": str(getattr(job, "title", "") or ""),
        "company": str(source_company_of(job) or ""),
        "description": str(getattr(job, "description", "") or ""),
        "location": getattr(job, "location", None),
        "contract_type": getattr(job, "contract_type", None),
        "remote_policy": getattr(job, "remote_policy", None),
        "application_url": getattr(job, "application_url", None),
        "source": str(getattr(job, "source", "") or ""),
        "source_data": getattr(job, "source_data", None),
    }


def hard_reject_input_sha256(job: HasJobFields) -> str:
    """Hash raw decision inputs with stable JSON ordering."""

    encoded = json.dumps(
        hard_reject_input_payload(job),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["hard_reject_input_payload", "hard_reject_input_sha256"]
