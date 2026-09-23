"""Prompt builder for application form questions."""

from __future__ import annotations

import json
from typing import Any

from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.profile import Profile


def build_system_prompt() -> str:
    """Load the current instructions when preparing a request."""
    return load_prompt("form_questions/system.j2")


def build_form_questions_prompt(
    *,
    profile: Profile,
    row: dict[str, Any],
    questions: str,
) -> tuple[str, str]:
    """Return system/user messages for form-question answer generation."""
    analysis = row.get("analysis_raw") if isinstance(row.get("analysis_raw"), dict) else {}
    analysis_json = json.dumps(analysis, ensure_ascii=False, indent=2)
    profile_json = profile.model_dump_json(indent=2)
    user = render_prompt(
        "form_questions/user.j2",
        application_id=row.get("id"),
        job_id=row.get("job_id"),
        title=row.get("title") or "",
        company=row.get("company") or "",
        location=row.get("job_location") or "",
        application_url=row.get("application_url") or "",
        job_description=row.get("job_description") or "",
        analysis_json=analysis_json,
        profile_json=profile_json,
        questions=questions.strip(),
    )
    return build_system_prompt(), user
