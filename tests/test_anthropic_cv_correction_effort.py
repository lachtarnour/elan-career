"""CV selection corrections reuse all original request settings without adding calls."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from smartapply.cv.adapter import CvAdapter
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.openai_provider import OpenAIProvider
from smartapply.profile import Project, get_profile
from tests.llm_config_helpers import configure_calls
from tests.test_cv import _sample_analysis
from tests.test_project_selection import draft


@pytest.mark.parametrize("provider_name", ["anthropic", "openai"])
@pytest.mark.parametrize("defect", [None, "project", "certificate"])
def test_cv_selection_correction_keeps_the_original_provider_model_effort_and_limit(
    isolated_db, monkeypatch, tmp_path, provider_name, defect
):
    profile = get_profile().model_copy(deep=True)
    profile.projects = [
        Project.model_validate(
            {
                "id": project_id,
                "name": project_id,
                "bullets": [],
                "selection_policy": {"tier": "primary"},
            }
        )
        for project_id in ("main1", "main2", "main3")
    ]
    valid = draft(["main1", "main2", "main3"])
    first = valid.model_copy(deep=True)
    if defect == "project":
        first = draft(["missing"])
    elif defect == "certificate":
        first.selected_certificate_ids = ["missing"]
    responses = [first, valid]
    calls = []
    model = "claude-sonnet-5-5" if provider_name == "anthropic" else "gpt-5.4"
    configure_calls(monkeypatch, tmp_path,
        cv_adaptation={
            "provider": provider_name, "model": model, "reasoning": "medium",
            "max_output_tokens": 12345,
        },
    )
    provider = (
        AnthropicProvider(model=model) if provider_name == "anthropic"
        else OpenAIProvider(model=model)
    )

    def call(**params):
        calls.append(copy.deepcopy(params))
        content = responses[len(calls) - 1].model_dump_json()
        if provider_name == "anthropic":
            return {
                "model": params["model"],
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": content}],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        return SimpleNamespace(
            model=params["model"],
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=content, refusal=None)
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(provider, f"_call_{provider_name}", call)
    cv = CvAdapter(profile, provider).adapt(
        _sample_analysis(),
        job_title="Data Scientist",
        job_company="Example",
        job_description="Extract entities from clinical records",
    )
    assert cv.selected_project_ids == ["main1", "main2", "main3"]
    assert cv.selected_certificate_ids == []
    assert len(calls) == (1 if defect is None else 2)
    if provider_name == "anthropic":
        assert all(params["model"] == "claude-sonnet-5-5" for params in calls)
        assert [params["output_config"]["effort"] for params in calls] == (
            ["medium"] * len(calls)
        )
        assert all(params["max_tokens"] == 12345 for params in calls)
        assert all("temperature" not in params for params in calls)
        users = [params["messages"][0]["content"] for params in calls]
    else:
        assert all("output_config" not in params for params in calls)
        assert all("temperature" not in params for params in calls)
        assert [params["reasoning_effort"] for params in calls] == (
            ["medium"] * len(calls)
        )
        assert all(params["max_completion_tokens"] == 12345 for params in calls)
        users = [params["messages"][1]["content"] for params in calls]
    if defect is not None:
        assert users[1].startswith(users[0])
        assert "Regenerate the complete CV selection, without a summary" in users[1]
        assert "missing" in users[1]
