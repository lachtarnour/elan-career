"""The JSON LLM catalogue is strict, editable, and visible without paid API calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from smartapply import config
from smartapply.desktop import llm_settings
from smartapply.llm_policy import INHERITED_CALLS, CallConfigurationError, resolve_route
from tests.llm_config_helpers import isolated_catalogue


@pytest.fixture
def llm_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=kept-openai\n", encoding="utf-8")
    (tmp_path / "model_config.json").write_text(
        json.dumps(isolated_catalogue()), encoding="utf-8"
    )
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(env))
    for name in config.Settings.model_fields:
        if name.startswith(("llm_", "openai_", "anthropic_")):
            monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("No API call is allowed in configuration tests")
    ))
    config.get_settings.cache_clear()
    yield env
    config.get_settings.cache_clear()


def edit_call(env: Path, purpose: str, **changes):
    path = env.with_name("model_config.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if changes.pop("remove_call", False):
        data.pop(purpose, None)
    else:
        data[purpose].update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"provider": ""}, "provider"),
        ({"provider": "invalid"}, "provider"),
        ({"model": ""}, "model"),
        ({"model": "gpt-5.4-mini"}, "model"),
        ({"model": "not-a-model"}, "model"),
        ({"reasoning": ""}, "reasoning"),
        ({"reasoning": "auto"}, "reasoning"),
        ({"reasoning": "none"}, "reasoning"),
        ({"max_output_tokens": None}, "max_output_tokens"),
        ({"max_output_tokens": 100001}, "max_output_tokens"),
    ],
)
def test_bad_call_field_identifies_step_and_json_property(llm_env, changes, field):
    edit_call(llm_env, "job_fit_assessment", **changes)
    with pytest.raises(CallConfigurationError) as error:
        resolve_route(config.get_llm_settings(), "job_fit_assessment")
    assert "Évaluation de l’adéquation" in str(error.value)
    assert f"job_fit_assessment.{field}" in str(error.value)


def test_missing_or_malformed_json_is_not_recreated(llm_env):
    path = llm_env.with_name("model_config.json")
    path.unlink()
    with pytest.raises(CallConfigurationError, match="model_config.json"):
        resolve_route(config.get_llm_settings(), "job_offer_extraction")
    assert not path.exists()
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Impossible de lire"):
        llm_settings.save_llm_calls([{
            "purpose": "job_fit_assessment", "provider": "openai", "model": "gpt-5.4-mini",
            "reasoning": "high", "max_output_tokens": 12000,
        }])
    assert path.read_text(encoding="utf-8") == "{broken"


def test_missing_call_is_not_filled_from_another_call(llm_env):
    unaffected = resolve_route(config.get_llm_settings(), "job_offer_extraction")
    edit_call(llm_env, "job_fit_assessment", remove_call=True)
    with pytest.raises(CallConfigurationError, match="job_fit_assessment.configuration"):
        resolve_route(config.get_llm_settings(), "job_fit_assessment")
    assert resolve_route(config.get_llm_settings(), "job_offer_extraction") == unaffected


@pytest.mark.parametrize(("purpose", "parent"), INHERITED_CALLS.items())
def test_correction_inherits_all_settings_and_ignores_legacy_overrides(llm_env, purpose, parent):
    path = llm_env.with_name("model_config.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[purpose] = {
        "provider": "anthropic", "model": "claude-opus-4-6",
        "reasoning": "max", "max_output_tokens": 99999,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    previous = config.get_llm_settings()
    original = resolve_route(previous, parent)
    assert resolve_route(previous, purpose) == original
    assert purpose not in {call["purpose"] for call in llm_settings.llm_configuration()["calls"]}
    llm_settings.save_llm_calls([{
        "purpose": parent, "provider": "anthropic", "model": "claude-haiku-5-5",
        "reasoning": "low", "max_output_tokens": 12345,
    }])
    current = config.get_llm_settings()
    route = resolve_route(current, purpose)
    assert route == resolve_route(current, parent)
    assert (route.provider, route.model, route.reasoning, route.max_output_tokens) == (
        "anthropic", "claude-haiku-5-5", "low", 12345,
    )
    assert resolve_route(previous, purpose) == original
    assert purpose not in json.loads(path.read_text(encoding="utf-8"))
    edit_call(llm_env, parent, remove_call=True)
    with pytest.raises(CallConfigurationError, match=f"{parent}.configuration"):
        resolve_route(config.get_llm_settings(), purpose)


def test_legacy_general_keys_are_rejected_in_json(llm_env):
    path = edit_call(llm_env, "job_fit_assessment", llm_provider="openai")
    with pytest.raises(CallConfigurationError, match="paramètres inconnus.*llm_provider"):
        resolve_route(config.get_llm_settings(), "job_fit_assessment")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["general"] = {"provider": "openai"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CallConfigurationError, match="appels inconnus.*general"):
        resolve_route(config.get_llm_settings(), "job_offer_extraction")


def test_json_save_is_independent_atomic_and_visible_next_call(llm_env):
    previous = config.get_llm_settings()
    previous_fit = resolve_route(previous, "job_fit_assessment")
    unaffected = resolve_route(previous, "job_offer_extraction")
    original_env = llm_env.read_text(encoding="utf-8")
    result = llm_settings.save_llm_calls([{
        "purpose": "job_fit_assessment", "provider": "anthropic", "model": "claude-haiku-5-5",
        "reasoning": "high", "max_output_tokens": 8192,
    }])
    assert result["model_config_path"] == str(llm_env.with_name("model_config.json"))
    assert resolve_route(config.get_llm_settings(), "job_fit_assessment").max_output_tokens == 8192
    assert resolve_route(previous, "job_fit_assessment") == previous_fit
    assert resolve_route(config.get_llm_settings(), "job_offer_extraction") == unaffected
    assert llm_env.read_text(encoding="utf-8") == original_env


def test_invalid_save_preserves_json_and_reports_field(llm_env):
    path = llm_env.with_name("model_config.json")
    original = path.read_bytes()
    with pytest.raises(CallConfigurationError, match="job_scope_screening.reasoning"):
        llm_settings.save_llm_calls([{
            "purpose": "job_scope_screening", "provider": "anthropic",
            "model": "claude-haiku-5-5", "reasoning": "auto", "max_output_tokens": 4096,
        }])
    assert path.read_bytes() == original


def test_json_write_failure_preserves_previous_file(llm_env, monkeypatch):
    from smartapply import model_config

    path = llm_env.with_name("model_config.json")
    previous = path.read_bytes()
    monkeypatch.setattr(model_config.os, "replace", lambda *args: (_ for _ in ()).throw(
        OSError("read-only destination")
    ))
    with pytest.raises(OSError, match="read-only destination"):
        llm_settings.save_llm_calls([{
            "purpose": "job_fit_assessment", "provider": "openai",
            "model": "gpt-5.4-mini", "reasoning": "high", "max_output_tokens": 11000,
        }])
    assert path.read_bytes() == previous
    assert not list(path.parent.glob(".model_config.json-*"))


def test_application_settings_write_only_env_and_hide_secrets(llm_env):
    shown = llm_settings.llm_configuration()
    before_route = resolve_route(config.get_llm_settings(), "job_fit_assessment")
    assert "kept-openai" not in repr(shown)
    assert shown["api_keys"]["openai"]
    llm_settings.save_application_settings({"LLM_CACHE_TTL_DAYS": "20"})
    assert "LLM_CACHE_TTL_DAYS='20'" in llm_env.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=kept-openai" in llm_env.read_text(encoding="utf-8")
    assert resolve_route(config.get_llm_settings(), "job_fit_assessment") == before_route
    llm_settings.save_llm_api_key("anthropic", "private-key")
    assert "ANTHROPIC_API_KEY='private-key'" in llm_env.read_text(encoding="utf-8")
    assert "private-key" not in repr(llm_settings.llm_configuration())


def test_invalid_application_value_does_not_change_env(llm_env):
    old = llm_env.read_bytes()
    with pytest.raises(ValueError, match="LLM_CACHE_TTL_DAYS"):
        llm_settings.save_application_settings({"LLM_CACHE_TTL_DAYS": "0"})
    assert llm_env.read_bytes() == old


def test_optional_context_window_can_be_cleared_from_env(llm_env):
    llm_settings.save_application_settings({"OPENAI_CONTEXT_WINDOW_TOKENS": "400000"})
    assert "OPENAI_CONTEXT_WINDOW_TOKENS='400000'" in llm_env.read_text(encoding="utf-8")
    llm_settings.save_application_settings({"OPENAI_CONTEXT_WINDOW_TOKENS": ""})
    assert "OPENAI_CONTEXT_WINDOW_TOKENS" not in llm_env.read_text(encoding="utf-8")


def test_explicit_transport_values_cannot_bypass_invalid_route(llm_env, monkeypatch):
    from pydantic import BaseModel

    from smartapply.llm.openai_provider import OpenAIProvider
    from smartapply.llm.provider import LLMError

    class Answer(BaseModel):
        value: str

    provider = OpenAIProvider(settings=config.get_llm_settings(), model="gpt-5.4-mini")
    monkeypatch.setattr(provider, "_generate", lambda _: (_ for _ in ()).throw(
        AssertionError("No provider request is allowed")
    ))
    edit_call(llm_env, "job_fit_assessment", reasoning="")
    provider._settings = config.get_llm_settings()
    with pytest.raises(LLMError, match="job_fit_assessment.reasoning"):
        provider.complete_json(
            system="test", user="test", schema=Answer, purpose="job_fit_assessment",
            model="gpt-5.4-mini", effort="medium", max_completion_tokens=12000,
        )


def test_direct_provider_reads_only_its_call_model(llm_env, isolated_db, monkeypatch):
    from pydantic import BaseModel

    from smartapply.llm.anthropic_provider import AnthropicProvider
    from smartapply.llm.structured import Completion

    class Answer(BaseModel):
        value: str

    provider = AnthropicProvider(settings=config.get_llm_settings())
    sent = []

    def generate(body):
        sent.append(body)
        return Completion(content='{"value":"ok"}', model=body["model"])

    monkeypatch.setattr(provider, "_generate", generate)
    assert provider.complete_json(
        system="s", user="u", schema=Answer, purpose="job_fit_assessment",
        use_cache=False,
    ).value == "ok"
    assert sent[0]["model"] == "claude-haiku-5-5"
    assert sent[0]["output_config"]["effort"] == "high"
    assert sent[0]["max_completion_tokens"] == 32000


@pytest.mark.parametrize(("purpose", "parent"), [
    ("job_fit_assessment", "job_fit_assessment"), *INHERITED_CALLS.items(),
])
def test_frozen_route_uses_its_complete_saved_settings_after_json_edit(
    llm_env, isolated_db, monkeypatch, purpose, parent
):
    from pydantic import BaseModel

    from smartapply.llm.factory import get_llm_provider
    from smartapply.llm.routes import frozen_provider
    from smartapply.llm.structured import Completion

    class Answer(BaseModel):
        value: str

    router = get_llm_provider()
    frozen = frozen_provider(
        router, "openai", "gpt-5.4-mini", purpose=purpose,
        reasoning="medium", max_output_tokens=12000,
    )
    edit_call(llm_env, parent, model="gpt-5.4", reasoning="high")
    bodies = []

    def generate(body):
        bodies.append(body)
        return Completion(content='{"value":"ok"}', model=body["model"])

    monkeypatch.setattr(frozen, "_generate", generate)
    assert frozen.complete_json(
        system="test", user="test", schema=Answer, purpose=purpose,
        model="gpt-5.4-mini", effort="medium", max_completion_tokens=12000,
        use_cache=False,
    ).value == "ok"
    assert bodies[0]["model"] == "gpt-5.4-mini"
    assert bodies[0]["reasoning_effort"] == "medium"
    assert bodies[0]["max_completion_tokens"] == 12000
    with pytest.raises(ValueError, match=f"{parent}.max_output_tokens"):
        frozen_provider(
            router, "openai", "gpt-5.4-mini", purpose=purpose,
            reasoning="medium", max_output_tokens=None,
        )
    with pytest.raises(ValueError, match=f"{parent}.reasoning"):
        frozen_provider(
            router, "openai", "gpt-5.4-mini", purpose=purpose,
            reasoning=None, max_output_tokens=12000,
        )


def test_analysis_preflight_blocks_missing_route_before_pipeline(llm_env, monkeypatch):
    from smartapply.desktop import analysis_services

    edit_call(llm_env, "job_scope_screening", provider="")
    monkeypatch.setattr(analysis_services, "ensure_startup_maintenance_complete", lambda: None)
    monkeypatch.setattr("smartapply.pipeline.Pipeline", lambda **kw: (_ for _ in ()).throw(
        AssertionError("Pipeline must not start")
    ))
    service = analysis_services.AnalysisServiceMixin()
    for method in (service.start_immediate_analysis, service.start_analysis_batch):
        with pytest.raises(CallConfigurationError, match="job_scope_screening.provider"):
            method([1])


def test_settings_page_shows_invalid_call_and_active_paths_without_network(tmp_path):
    qml = Path(__file__).resolve().parent.parent / "smartapply/desktop/qml"
    template = tmp_path / "model_config.json"
    template.write_text(json.dumps(isolated_catalogue()), encoding="utf-8")
    script = r'''
import json, os, socket, sys, tempfile
from pathlib import Path
runtime=tempfile.TemporaryDirectory(prefix="elan-config-ui-")
folder=Path(runtime.name)
os.environ.update(ELAN_HOME=str(folder), ELAN_ENV_FILE=str(folder/".env"),
    DATABASE_URL=f"sqlite:///{folder/'test.db'}", OUTPUT_DIR=str(folder/"output"),
    CACHE_DIR=str(folder/"cache"), PROFILE_DIR=str(Path("tests/fixtures/profile").resolve()),
    OPENAI_API_KEY="", ANTHROPIC_API_KEY="", APIFY_TOKEN="", SERPAPI_API_KEY="",
    FRANCETRAVAIL_CLIENT_ID="", FRANCETRAVAIL_CLIENT_SECRET="", WTTJ_COOKIE="")
(folder/".env").write_text("OPENAI_API_KEY=kept-openai\n")
choices=json.loads(Path(sys.argv[2]).read_text())
choices["job_fit_assessment"].pop("provider")
(folder/"model_config.json").write_text(json.dumps(choices))
def deny(*a, **k): raise AssertionError("No network in UI test")
socket.create_connection=deny; socket.socket.connect=deny
from PySide6.QtCore import QElapsedTimer, QEventLoop, QMetaObject, QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.services import DesktopService
app=QGuiApplication([])
service=DesktopService(); service.sync_analysis_batches=lambda: {"candidates":[],"runs":[],"active_run_id":""}
bridge=DesktopBridge(service)
engine=QQmlEngine(); engine.rootContext().setContextProperty("AppBridge",bridge)
engine.addImportPath(sys.argv[1])
component=QQmlComponent(engine,QUrl.fromLocalFile(str(Path(sys.argv[1])/"pages/SettingsPage.qml")))
page=component.create()
assert page is not None, "\n".join(x.toString() for x in component.errors())
page.setProperty("width",1200); page.setProperty("height",900)
host=QQuickWindow();host.resize(1200,900)
page.setParentItem(host.contentItem());host.show()
def wait(predicate):
    timer=QElapsedTimer();timer.start()
    while not predicate() and timer.elapsed()<5000: app.processEvents(QEventLoop.AllEvents,20)
    assert predicate(), "UI update timeout"
def find(item,name):
    if item.objectName()==name:return item
    for child in item.childItems():
        result=find(child,name)
        if result:return result
    return None
wait(lambda: len(bridge.llmConfiguration.get("calls",[]))==7)
assert find(page,"llmCall_motivation_letter_repair") is None
assert find(page,"llmCall_cv_selection_correction") is None
error=find(page,"llmConfigurationError_job_fit_assessment")
assert error is not None and "job_fit_assessment.provider" in error.property("text")
assert page.property("configurationIncomplete")
assert str(folder/"model_config.json") in bridge.llmConfiguration["model_config_path"]
fit_editor=find(page,"llmCall_job_fit_assessment")
assert fit_editor is not None
fit_editor.setProperty("provider","anthropic")
fit_editor.setProperty("modelName","claude-haiku-5-5")
assert QMetaObject.invokeMethod(fit_editor,"applyRecommendedSettings")
assert fit_editor.property("reasoning")=="high"
assert fit_editor.property("maxOutputTokens")=="32768"
fit_editor.setProperty("provider","openai")
fit_editor.setProperty("modelName","gpt-5.4-mini")
assert QMetaObject.invokeMethod(fit_editor,"applyRecommendedSettings")
assert fit_editor.property("reasoning")=="medium"
assert fit_editor.property("maxOutputTokens")=="32000"
cv_editor=find(page,"llmCall_cv_adaptation")
assert cv_editor is not None
cv_editor.setProperty("provider","anthropic")
cv_editor.setProperty("modelName","claude-haiku-5-5")
assert QMetaObject.invokeMethod(cv_editor,"applyRecommendedSettings")
assert cv_editor.property("reasoning")=="high"
assert cv_editor.property("maxOutputTokens")=="32000"
# Only one model control is visible; custom IDs replace the suggested list.
page.setProperty("expandedCall","job_fit_assessment")
app.processEvents()
model_select=find(page,"llmModelSelect_job_fit_assessment")
custom_model=find(page,"llmModel_job_fit_assessment")
assert model_select.property("visible") and not custom_model.property("visible")
model_select.setProperty("currentIndex",3)
model_select.activated.emit(3)
app.processEvents()
assert custom_model.property("visible") and not model_select.property("visible")
custom_model.setProperty("text","custom-model-v1")
custom_model.textEdited.emit()
assert not fit_editor.property("valid")
custom_model.setProperty("text","gpt-custom-v1")
custom_model.textEdited.emit()
reasoning_select=find(page,"llmReasoning_job_fit_assessment")
reasoning_select.setProperty("currentIndex",0)
reasoning_select.activated.emit(0)
assert fit_editor.property("valid")
assert page.property("callsEdited") and page.property("callsValid")
# Passive refresh and navigation keep a dirty editor and its exact pending value.
page.setProperty("callGroup","documents")
page.setProperty("selectedSection","connections")
bridge.llmConfigurationChanged.emit()
app.processEvents()
assert fit_editor.property("modelName")=="gpt-custom-v1"
page.setProperty("selectedSection","ai")
page.setProperty("callGroup","analysis")
app.processEvents()
find(page,"saveLlmCallsButton").clicked.emit()
wait(lambda: json.loads((folder/"model_config.json").read_text())["job_fit_assessment"]["model"]=="gpt-custom-v1")
wait(lambda: not bridge.llmConfigurationSaving and not page.property("callsEdited"))
assert not page.property("configurationIncomplete")
saved=json.loads((folder/"model_config.json").read_text())
assert saved["job_fit_assessment"]["reasoning"]=="auto"
assert saved["job_fit_assessment"]["max_output_tokens"]==32000
review_editor=find(page,"llmCall_document_review")
assert review_editor is not None
review_editor.setProperty("provider","openai")
review_editor.setProperty("modelName","gpt-5.4")
assert QMetaObject.invokeMethod(review_editor,"applyRecommendedSettings")
assert review_editor.property("reasoning")=="low"
correction_editor=find(page,"llmCall_document_correction")
assert correction_editor is not None
assert review_editor.property("maxOutputTokens")=="32000"
nav=find(page,"settingsNav_app")
assert nav is not None
nav.chosen.emit();app.processEvents()
assert page.property("selectedSection")=="app"
assert page.findChild(QObject,"llmApiKeyField") is not None
assert find(page,"appSetting_LLM_CACHE_TTL_DAYS") is not None
assert not find(page,"llmApiKeyField").property("visible")
find(page,"settingsNav_connections").chosen.emit();app.processEvents()
assert find(page,"llmApiKeyField").property("visible")
masked=QQmlExpression(engine.rootContext(),find(page,"llmApiKeyField"),"Number(echoMode) === 2")
assert masked.evaluate()[0] and not masked.hasError(), masked.error().toString()
# Every editable setting remains reachable in its group, including collapsed ones.
for field in bridge.llmConfiguration["app_fields"]:
    assert find(page,"appSetting_"+field["key"]) is not None, field["key"]
cache_field=find(page,"appSetting_LLM_CACHE_TTL_DAYS")
cache_field.setProperty("text","19")
cache_field.textEdited.emit()
page.setProperty("selectedSection","files")
bridge.llmConfigurationChanged.emit();app.processEvents()
find(page,"saveApplicationSettingsButton").clicked.emit()
wait(lambda: "LLM_CACHE_TTL_DAYS='19'" in (folder/".env").read_text())
wait(lambda: not bridge.llmConfigurationSaving)
# Cancelling a focused text area restores its saved value without creating a new draft.
page.setProperty("selectedSection","app");app.processEvents()
experience=find(page,"appSetting_DECLARED_EXPERIENCE")
original_experience=experience.property("text")
experience.forceActiveFocus();app.processEvents()
assert experience.property("activeFocus")
experience.setProperty("text","Une expérience modifiée")
app.processEvents()
pending=QQmlExpression(engine.rootContext(),page,"Object.keys(appEdits).length")
assert pending.evaluate()[0]==1
find(page,"discardSettingsButton").clicked.emit();app.processEvents()
assert pending.evaluate()[0]==0
assert experience.property("text")==original_experience
# Section layouts and the fixed save action stay within a narrower viewport.
page.setProperty("width",760)
host.resize(760,900)
for section in ("ai","connections","app","files"):
    page.setProperty("selectedSection",section);app.processEvents()
    save=find(page,"saveLlmCallsButton" if section=="ai" else "saveApplicationSettingsButton")
    assert save.property("visible") and save.width()>0
    wait(lambda: save.mapToItem(page,save.boundingRect().bottomRight()).x()<=page.width())
analysis_component=QQmlComponent(engine,QUrl.fromLocalFile(str(Path(sys.argv[1])/"pages/AnalysisPage.qml")))
analysis=analysis_component.create()
assert analysis is not None, "\n".join(x.toString() for x in analysis_component.errors())
bridge._analysis_controller._failed(
    "Configuration IA — Évaluation de l’adéquation : modèle manquant (model_config.json → job_fit_assessment.model).", ""
)
app.processEvents()
analysis_error=find(analysis,"analysisWorkspaceError")
assert analysis_error is not None and "job_fit_assessment.model" in analysis_error.property("text")
bridge.requestShutdown();wait(lambda:bridge.shutdownReady)
host.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(qml), str(template)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
             "QSG_RHI_BACKEND": "software", "QT_QUICK_CONTROLS_STYLE": "Basic"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_packaged_first_launch_initializes_once_and_never_repairs_deleted_json():
    root = Path(__file__).resolve().parent.parent
    script = r'''
import os, sys, tempfile
from pathlib import Path
home=Path(tempfile.mkdtemp(prefix="elan-config-first-launch-"))
os.environ["ELAN_HOME"]=str(home)
os.environ["ELAN_ENV_FILE"]=str(home/".env")
sys.frozen=True
from smartapply.desktop.bootstrap import prepare_runtime_environment
first=prepare_runtime_environment()
assert first.packaged and (home/".env").is_file()
model=home/"model_config.json"
assert model.is_file()
model.write_text('{"custom": true}')
prepare_runtime_environment()
assert model.read_text()=='{"custom": true}'
model.unlink()
prepare_runtime_environment()
assert not model.exists()
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
