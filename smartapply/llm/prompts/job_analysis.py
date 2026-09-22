"""Offer-only extraction prompts for the first LLM call after filtering."""

from __future__ import annotations

from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.offers import AnalyzerInput
from smartapply.profile import Profile


def build_system_prompt() -> str:
    """Load the current instructions when preparing a request."""
    return load_prompt("job_analysis/system_long.j2")


def build_user_prompt(
    *,
    profile: Profile | None = None,
    job_title: str,
    job_description: str,
    job_company: str = "",
    job_location: str | None = None,
    application_url: str | None = None,
    source_metadata: str | None = None,
) -> str:
    """Extract the offer independently; ``profile`` is an ignored legacy argument."""
    source_metadata_block = _source_metadata_block(source_metadata)
    return render_prompt(
        "job_analysis/user.j2",
        job_title=job_title,
        job_company=job_company,
        job_location=job_location or "",
        application_url=application_url or "",
        source_metadata_block=source_metadata_block,
        job_description=job_description.strip(),
    )


def build_user_prompt_from_input(
    *,
    analyzer_input: AnalyzerInput,
    profile: Profile | None = None,
) -> str:
    """Build the offer-only prompt from canonical input, ignoring legacy profile data."""
    return build_user_prompt(
        job_title=analyzer_input.title,
        job_company=analyzer_input.company,
        job_location=analyzer_input.location,
        application_url=analyzer_input.application_url,
        source_metadata=analyzer_input.source_metadata,
        job_description=analyzer_input.offer_body,
    )


def _source_metadata_block(source_metadata: str | None) -> str:
    metadata = (source_metadata or "").strip()
    if not metadata:
        return ""
    return render_prompt("job_analysis/source_metadata_block.j2", metadata=metadata)
