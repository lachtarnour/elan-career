"""Independent offer-only requests and durable prefilter attempts."""

from __future__ import annotations

from smartapply.database.scope_checkpoints import ScopeCheckpoint
from smartapply.llm.batch import build_batch_request
from smartapply.llm.prompts import job_scope_screening
from smartapply.llm.request_options import stage_effort
from smartapply.llm.schemas import JobScopeScreening


def scope_context(*, provider, model, settings, max_completion_tokens, effort="auto"):
    return {
        "provider": provider,
        "model": model,
        "effort": (
            stage_effort(provider, model, "job_scope_screening", settings=settings)
            if effort == "auto"
            else effort
        ),
        "max_completion_tokens": max_completion_tokens,
        "prompt_version": job_scope_screening.PROMPT_VERSION,
        "system_prompt": job_scope_screening.build_system_prompt(),
    }


def scope_request(context, analyzer_input, *, custom_id="screening"):
    return build_batch_request(
        custom_id=custom_id,
        system=context["system_prompt"],
        user=job_scope_screening.build_user_prompt(offer_body=analyzer_input.offer_body),
        schema=JobScopeScreening,
        model=context["model"],
        provider=context["provider"],
        effort=context.get("effort"),
        purpose="job_scope_screening",
        max_completion_tokens=context["max_completion_tokens"],
    )


def scope_result(value=None, *, error=None, context=None):
    """Invalid classifications and API failures never authorize rejection."""
    if error is not None:
        return {
            "status": "fail_open",
            "mission_screen": None,
            "error": str(error),
            "context": context,
        }
    try:
        parsed = JobScopeScreening.model_validate(value)
    except ValueError as exc:
        return scope_result(error=exc, context=context)
    return {"status": "valid", **parsed.model_dump(mode="json"), "error": "", "context": context}


def save_scope(session, job_id, key, data):
    checkpoint = session.get(ScopeCheckpoint, (job_id, key))
    if checkpoint is None:
        checkpoint = ScopeCheckpoint(job_id=job_id, screening_key=key)
        session.add(checkpoint)
    checkpoint.screening_key = key
    checkpoint.result_data = data


def load_scope(session, job_id, key):
    checkpoint = session.get(ScopeCheckpoint, (job_id, key))
    if checkpoint and checkpoint.screening_key == key:
        data = checkpoint.result_data
        if isinstance(data, dict) and data.get("status") == "valid":
            return data
    return None
