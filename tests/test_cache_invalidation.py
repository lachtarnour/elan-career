"""Edits must be visible to the next operation without flushing persisted caches."""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from smartapply.llm.prompts import loader


@pytest.fixture
def editable_prompts(tmp_path, monkeypatch):
    package = tmp_path / "prompts"
    shutil.copytree(Path(loader.__file__).parent / "templates", package / "templates")
    monkeypatch.setattr(loader, "files", lambda _package: package)
    return package / "templates"


@pytest.mark.parametrize(
    "name,filename",
    [
        ("job_analysis", "system_long.j2"),
        ("job_fit", "system.j2"),
        ("cv_adaptation", "system.j2"),
        ("application_draft", "system.j2"),
        ("motivation_letter", "system.j2"),
        ("application_texts", "system.j2"),
        ("document_review", "system.j2"),
        ("motivation_letter_repair", "system.j2"),
        ("form_questions", "system.j2"),
    ],
)
def test_system_instructions_reload_without_reimport(editable_prompts, name, filename):
    module = importlib.import_module(f"smartapply.llm.prompts.{name}")
    before = module.build_system_prompt()
    path = editable_prompts / name / filename
    path.write_text(path.read_text() + "\nUpdated instruction.\n")
    assert module.build_system_prompt() == before + "\nUpdated instruction.\n"


def test_shared_summary_rules_and_user_template_reload(editable_prompts):
    from smartapply.llm.prompts import application_texts

    before = application_texts.build_system_prompt()
    rules = editable_prompts / "cv_adaptation" / "summary_rules.j2"
    rules.write_text(rules.read_text() + "\nNew shared summary requirement.\n")
    after = application_texts.build_system_prompt()
    assert after != before
    assert "New shared summary requirement." in after
    assert "New shared summary requirement." in loader.load_prompt("cv_adaptation/summary_rules.j2")
    user = editable_prompts / "document_review" / "user.j2"
    user.write_text("Old {{ value }}")
    assert loader.render_prompt("document_review/user.j2", value="input") == "Old input"
    user.write_text("New {{ value }}")
    assert loader.render_prompt("document_review/user.j2", value="input") == "New input"


def test_next_pipeline_loads_edited_profile_without_restart(tmp_path, monkeypatch):
    from smartapply.config import get_settings
    from smartapply.pipeline import Pipeline
    from smartapply.profile import get_profile

    profile_dir = tmp_path / "profile"
    shutil.copytree(get_settings().profile_dir, profile_dir)
    monkeypatch.setenv("PROFILE_DIR", str(profile_dir))
    get_settings.cache_clear()
    try:
        before = Pipeline()
        assert get_profile() is before.profile
        path = profile_dir / "skills.json"
        data = json.loads(path.read_text())
        data["categories"][0]["skills"].append("New declared skill")
        path.write_text(json.dumps(data))
        after = Pipeline()
        assert "New declared skill" not in before.profile.skills.allowed_skills
        assert "New declared skill" in after.profile.skills.allowed_skills
        assert after.scorer.profile is after.profile
        assert after.adapter.profile is after.profile
        assert get_profile() is after.profile
    finally:
        get_settings.cache_clear()


def test_embeddings_recompute_only_changed_text_or_model(isolated_db, monkeypatch):
    from smartapply.ranking.embeddings import OpenAIEmbeddingsProvider

    calls = []

    def fake_create(self, client, batch):
        calls.append((self.model_name, list(batch)))
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=i, embedding=[len(text), 1.0]) for i, text in enumerate(batch)
            ],
            usage=SimpleNamespace(prompt_tokens=1),
        )

    monkeypatch.setattr(OpenAIEmbeddingsProvider, "_create_embeddings", fake_create)
    first = OpenAIEmbeddingsProvider(model="model-a")
    first._client = object()
    first.embed(["profile old", "project unchanged"])
    first.embed(["profile new", "project unchanged"])
    first.embed(["profile new", "project unchanged"])
    other = OpenAIEmbeddingsProvider(model="model-b")
    other._client = object()
    other.embed(["profile new", "project unchanged"])
    assert calls == [
        ("model-a", ["profile old", "project unchanged"]),
        ("model-a", ["profile new"]),
        ("model-b", ["profile new", "project unchanged"]),
    ]


def test_edited_prompt_bypasses_old_llm_response(editable_prompts, isolated_db, monkeypatch):
    from smartapply.llm.openai_provider import OpenAIProvider
    from smartapply.llm.prompts import application_texts

    class Answer(BaseModel):
        value: str

    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"value": f"response-{len(calls)}"}))
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    provider = OpenAIProvider()
    monkeypatch.setattr(provider, "_call_openai", fake_call)

    def request():
        return provider.complete_json(
            system=application_texts.build_system_prompt(),
            user="Same profile and offer",
            schema=Answer,
        ).value

    assert request() == "response-1"
    assert request() == "response-1"
    path = editable_prompts / "application_texts" / "system.j2"
    path.write_text(path.read_text() + "\nA new writing instruction.\n")
    assert request() == "response-2"
    assert request() == "response-2"
    assert len(calls) == 2
