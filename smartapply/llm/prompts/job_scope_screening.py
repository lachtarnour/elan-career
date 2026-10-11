"""The original mission instructions, isolated from factual extraction."""

from smartapply.llm.prompts.loader import load_prompt, render_prompt

PROMPT_VERSION = "job_scope_screening_v1"


def build_system_prompt() -> str:
    return load_prompt("job_scope_screening/system.j2")


def build_user_prompt(*, offer_body: str) -> str:
    return render_prompt("job_scope_screening/user.j2", offer_body=offer_body.strip())
