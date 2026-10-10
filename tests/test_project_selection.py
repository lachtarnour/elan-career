"""Policy enforcement, bounded regeneration and snapshot compatibility."""

from __future__ import annotations

import json

import pytest

from smartapply.config import get_llm_settings
from smartapply.cv.adapter import CvAdapter
from smartapply.cv.assembly import assemble_cv
from smartapply.cv.certificate_selection import certificate_policy_errors
from smartapply.cv.project_selection import validate_project_selection
from smartapply.cv.role_contracts import apply_contract
from smartapply.cv.validator import CvValidator
from smartapply.llm import CVPersonalization, CVSelection, JobAnalysis, LLMError, MockLLMProvider
from smartapply.llm.openai_provider import _to_strict_schema
from smartapply.profile import Project, get_profile
from tests.llm_config_helpers import isolated_catalogue


@pytest.fixture
def inputs():
    profile = get_profile().model_copy(deep=True)
    profile.projects = [
        Project.model_validate(
            {
                "id": pid,
                "name": pid,
                "bullets": [],
                "selection_policy": {
                    "tier": tier,
                    "activation_conditions": ["Entity extraction"] if tier == "specialized" else [],
                },
            }
        )
        for pid, tier in [
            ("main1", "primary"),
            ("main2", "primary"),
            ("main3", "primary"),
            ("special1", "specialized"),
            ("special2", "specialized"),
            ("off", "disabled"),
        ]
    ]
    analysis = JobAnalysis(
        role_type="Data Scientist",
        seniority="mid",
        domain="NLP",
        main_tasks=["Extract entities from clinical records"],
        required_skills=["Python"],
        nice_to_have=[],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=["RAG"],
    )
    return profile, analysis


def draft(ids):
    return CVSelection(
        cv_title="Data Scientist",
        selected_certificate_ids=[],
        selected_project_ids=ids,
        project_selection_reasons=[
            {
                "project_id": pid,
                "reason": "Demonstrates extraction from clinical text",
                "offer_evidence": "Extract entities from clinical records",
                "complementary_value": "Direct entity extraction evidence",
            }
            for pid in ids
        ],
        skills_order=[],
        warnings=[],
    )


@pytest.mark.parametrize("ids", [["main1", "main2", "main3"], ["main3", "main1", "special1"]])
def test_valid_policy_requires_three_distinct_allowed_projects(inputs, ids):
    assert validate_project_selection(draft(ids), *inputs) == []


@pytest.mark.parametrize("ids", [[], ["main1"], ["special1"], ["main1", "special1"]])
def test_short_selections_are_rejected_without_adding_projects(inputs, ids):
    response = draft(ids)
    before = response.model_dump()
    assert "too_few_projects:min=3" in validate_project_selection(response, *inputs)
    assert response.model_dump() == before
    cv = assemble_cv(response, inputs[0])
    validator = CvValidator(inputs[0])
    assert "too_few_projects:min=3" in validator.validate(cv).errors
    fixed, _ = validator.auto_fix(cv)
    assert fixed.selected_project_ids == ids


def test_duplicate_does_not_count_toward_minimum(inputs):
    errors = validate_project_selection(draft(["main1", "main2", "main1"]), *inputs)
    assert "too_few_projects:min=3" in errors
    assert "duplicate_project_id:main1" in errors


@pytest.mark.parametrize(
    ("ids", "error"),
    [
        (["missing"], "unknown_project_id"),
        (["off"], "disabled_project_id"),
        (["main1", "main1"], "duplicate_project_id"),
        (["special1", "special2"], "too_many_specialized_projects"),
        (["main1", "main2", "main3", "special1"], "too_many_projects"),
    ],
)
def test_invalid_policy_is_reported_without_mutating_response(inputs, ids, error):
    response = draft(ids)
    before = response.model_dump()
    assert any(e.startswith(error) for e in validate_project_selection(response, *inputs))
    assert response.model_dump() == before


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"reason": " "}, "missing_project_reason"),
        ({"offer_evidence": "RAG"}, "ungrounded_project_evidence"),
        ({"offer_evidence": "Invented offer demand"}, "ungrounded_project_evidence"),
        ({"complementary_value": " "}, "missing_complementary_value"),
    ],
)
def test_specialized_requires_grounded_reasons(inputs, change, error):
    response = draft(["special1", "main1", "main2"])
    response.project_selection_reasons[0] = response.project_selection_reasons[0].model_copy(
        update=change
    )
    assert any(e.startswith(error) for e in validate_project_selection(response, *inputs))


def test_raw_offer_quote_and_whitespace_are_supported(inputs):
    response = draft(["special1", "main1", "main2"])
    response.project_selection_reasons[0].offer_evidence = "Extraction d’entités cliniques"
    assert not validate_project_selection(
        response, *inputs, job_description="Mission : Extraction   d’entités\ncliniques."
    )


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("Développer des agents à partir d’\nAPIs", "Développer des agents à partir d’APIs"),
        ("Extraction d' entités cliniques", "Extraction d’entités cliniques"),
        ("Extraction d \u202f’ \t entités cliniques", "Extraction d'entités cliniques"),
        ("Lʼintégration d’APIs", "L'intégration d'APIs"),
        ("Analyses\u00a0de\tse\u0301ries\ntemporelles", "analyses de séries temporelles"),
        ("Construire des micro‑services", "Construire des micro-services"),
        ("Construire des micro‐services", "Construire des micro-services"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_offer_evidence_accepts_typographic_variants_without_rewriting(
    inputs, source, quote, reverse
):
    if reverse:
        source, quote = quote, source
    response = draft(["main1", "main2", "main3"])
    response.project_selection_reasons[0].offer_evidence = quote
    before = response.model_dump()
    assert validate_project_selection(response, *inputs, job_description=source) == []
    assert response.model_dump() == before


@pytest.mark.parametrize("source_field", ["main_tasks", "required_skills", "job_description"])
def test_offer_evidence_uses_same_normalization_for_each_allowed_source(inputs, source_field):
    profile, analysis = inputs
    source = "Développer d’\nAPIs"
    response = draft(["main1", "main2", "main3"])
    response.project_selection_reasons[0].offer_evidence = "Développer d'APIs"
    kwargs = {}
    if source_field == "job_description":
        kwargs[source_field] = source
    else:
        getattr(analysis, source_field).append(source)
    assert validate_project_selection(response, profile, analysis, **kwargs) == []


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("Concevoir un POC", "Concevoir une solution de production"),
        ("Déploiement non requis", "Déploiement requis"),
        ("Python et SQL", "SQL et Python"),
        ("Expérience de 3 ans", "Expérience de 5 ans"),
        ("Développer d'APIs", "Développer dAPIs"),
        ("Modèle créé", "Modele cree"),
        ("C++ et SQL", "C et SQL"),
        ("Data science", "Datascience"),
        ("re-sign", "resign"),
        ("Valeur −1", "Valeur -1"),
        ("Développer d’APIs", " \u00a0\t\n "),
    ],
)
def test_offer_evidence_still_rejects_content_changes(inputs, source, quote):
    response = draft(["main1", "main2", "main3"])
    response.project_selection_reasons[0].offer_evidence = quote
    assert validate_project_selection(response, *inputs, job_description=source) == [
        "ungrounded_project_evidence:main1"
    ]


def test_specialized_without_activation_conditions_is_invalid(inputs):
    profile, analysis = inputs
    profile.projects[3].selection_policy.activation_conditions = []
    assert "missing_activation_conditions:special1" in validate_project_selection(
        draft(["special1", "main1", "main2"]), profile, analysis
    )


@pytest.mark.parametrize(
    "reason_ids", [[], ["main1", "main2"], ["main1", "main2", "special1"], ["main1"] * 3]
)
def test_reasons_must_correspond_one_to_one_to_selection(inputs, reason_ids):
    response = draft(["main1", "main2", "main3"])
    response.project_selection_reasons = draft(reason_ids).project_selection_reasons
    assert validate_project_selection(response, *inputs)


class SequenceProvider(MockLLMProvider):
    def __init__(self, responses):
        super().__init__({})
        self.responses = iter(responses)
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


@pytest.mark.parametrize(
    "quote",
    [
        "Développer des agents IA à partir : d’APIs de modèles de langage, de frameworks agentiques",
        "Développer des agents IA à partir : d’APIs de modèles de langage,",
    ],
)
def test_source_line_break_after_apostrophe_does_not_trigger_selection_retry(inputs, quote):
    profile, analysis = inputs
    response = draft(["main1", "main2", "main3"])
    response.project_selection_reasons[0].offer_evidence = quote
    before = response.model_dump()
    provider = SequenceProvider([response])
    cv = CvAdapter(profile, provider).adapt(
        analysis,
        job_title="AI Engineer",
        job_company="Example",
        job_description=(
            "Développer des agents IA à partir : d’\n"
            "APIs de modèles de langage, de frameworks agentiques"
        ),
    )
    assert len(provider.calls) == 1
    assert response.model_dump() == before
    assert cv.selected_project_ids == response.selected_project_ids


def test_invalid_selection_retries_structure_before_summary_and_keeps_final_order(inputs):
    profile, analysis = inputs
    provider = SequenceProvider(
        [
            draft(["off"]),
            draft(["main2", "special1", "main1"]),
        ]
    )
    adapter = CvAdapter(profile, provider)
    cv = adapter.adapt(analysis, job_title="Data Scientist", job_company="Example")
    assert len(provider.calls) == 2
    assert "disabled_project_id:off" in provider.calls[1]["user"]
    assert provider.calls[1]["refresh_cache"] is True
    assert all(call["schema"] is CVSelection for call in provider.calls)
    assert "professional_summary" not in cv.model_dump()
    assert cv.selected_project_ids == ["main2", "special1", "main1"]
    assert [r.project_id for r in cv.project_selection_reasons] == cv.selected_project_ids


@pytest.mark.parametrize(
    ("provider_name", "model"),
    [("openai", "gpt-5.4"), ("anthropic", "claude-sonnet-5-5")],
)
def test_cv_initial_and_correction_keep_distinct_efforts_in_one_snapshot(
    inputs, monkeypatch, tmp_path, provider_name, model
):
    profile, analysis = inputs
    calls = isolated_catalogue()
    calls["cv_adaptation"].update(
        provider=provider_name, model=model, reasoning="low", max_output_tokens=12345
    )
    calls["document_correction"].update(
        provider=provider_name, model=model, reasoning="medium", max_output_tokens=16000
    )
    path = tmp_path / "model_config.json"
    path.write_text(json.dumps(calls), encoding="utf-8")
    monkeypatch.setattr("smartapply.model_config.model_config_file", lambda: path)

    class ConfiguredSequenceProvider(SequenceProvider):
        @property
        def smart_model(self):
            return model

    provider = ConfiguredSequenceProvider([draft(["off"]), draft(["main2", "special1", "main1"])])
    provider.name = provider_name
    provider._settings = get_llm_settings()
    original_complete = provider.complete_json

    def complete(**kwargs):
        if not provider.calls:
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["cv_adaptation"].update(reasoning="high", max_output_tokens=4096)
            changed["document_correction"].update(reasoning="high", max_output_tokens=4096)
            path.write_text(json.dumps(changed), encoding="utf-8")
        return original_complete(**kwargs)

    monkeypatch.setattr(provider, "complete_json", complete)
    cv = CvAdapter(profile, provider).adapt(
        analysis, job_title="Data Scientist", job_company="Example"
    )
    assert cv.selected_project_ids == ["main2", "special1", "main1"]
    assert [call["effort"] for call in provider.calls] == ["low", "medium"]
    assert [call["model"] for call in provider.calls] == [model, model]
    assert [call["max_completion_tokens"] for call in provider.calls] == [12345, 16000]
    assert [call["purpose"] for call in provider.calls] == ["cv_adaptation", "cv_selection_correction"]


def test_invalid_second_attempt_stops_before_letter(inputs):
    profile, analysis = inputs
    provider = SequenceProvider([draft(["off"]), draft(["off"])])
    adapter = CvAdapter(profile, provider)
    with pytest.raises(LLMError, match="Project selection could not be finalized"):
        adapter.adapt_application(analysis, job_title="Data Scientist", job_company="Example")
    assert [c["purpose"] for c in provider.calls] == ["cv_adaptation", "cv_selection_correction"]


def test_normal_generation_uses_one_call_and_preserves_reasons_in_snapshot(inputs):
    profile, analysis = inputs
    provider = SequenceProvider([draft(["main1", "special1", "main2"])])
    cv = CvAdapter(profile, provider).adapt(
        analysis, job_title="Data Scientist", job_company="Example"
    )
    assert len(provider.calls) == 1
    assert type(cv).model_validate_json(cv.model_dump_json()) == cv


@pytest.mark.parametrize("count", [0, 1, 2])
def test_valid_certificate_counts_use_the_project_selection_call(inputs, count):
    profile, analysis = inputs
    ids = [certificate.id for certificate in profile.certificates[:count]]
    response = draft(["main1", "main2", "main3"]).model_copy(
        update={"selected_certificate_ids": ids}
    )
    provider = SequenceProvider([response])

    cv = CvAdapter(profile, provider).adapt(
        analysis, job_title="Data Scientist", job_company="Example"
    )

    assert len(provider.calls) == 1
    assert cv.selected_certificate_ids == ids
    assert certificate_policy_errors(ids, profile) == []


@pytest.mark.parametrize(
    ("ids", "error"),
    [
        (["missing"], "unknown_certificate_id:missing"),
        (["cert_rag_dlai", "cert_rag_dlai"], "duplicate_certificate_id:cert_rag_dlai"),
        (["cert_rag_dlai", "cert_foundation_langchain", "missing"], "too_many_certificates:max=2"),
    ],
)
def test_invalid_certificate_selection_retries_with_same_cv_schema(inputs, ids, error):
    profile, analysis = inputs
    project_ids = ["main1", "main2", "main3"]
    first = draft(project_ids).model_copy(update={"selected_certificate_ids": ids})
    final = draft(project_ids).model_copy(update={"selected_certificate_ids": []})
    provider = SequenceProvider([first, final])

    cv = CvAdapter(profile, provider).adapt(
        analysis, job_title="Data Scientist", job_company="Example"
    )

    assert [call["purpose"] for call in provider.calls] == ["cv_adaptation", "cv_selection_correction"]
    assert [call["schema"] for call in provider.calls] == [CVSelection] * 2
    assert error in provider.calls[1]["user"]
    assert provider.calls[1]["refresh_cache"] is True
    assert cv.selected_certificate_ids == []


def test_second_invalid_certificate_selection_stops_generation(inputs):
    profile, analysis = inputs
    invalid = draft(["main1", "main2", "main3"]).model_copy(
        update={"selected_certificate_ids": ["missing"]}
    )
    provider = SequenceProvider([invalid, invalid])
    with pytest.raises(LLMError, match="unknown_certificate_id:missing"):
        CvAdapter(profile, provider).adapt(
            analysis, job_title="Data Scientist", job_company="Example"
        )
    assert len(provider.calls) == 2


def test_final_cv_validator_rejects_invalid_certificate_ids_without_rewriting(inputs):
    profile, _ = inputs
    invalid = draft(["main1", "main2", "main3"]).model_copy(
        update={"selected_certificate_ids": ["missing"]}
    )
    cv = assemble_cv(invalid, profile)
    validator = CvValidator(profile)

    assert "unknown_certificate_id:missing" in validator.validate(cv).errors
    fixed, _ = validator.auto_fix(cv)
    assert fixed.selected_certificate_ids == ["missing"]
    assert "unknown_certificate_id:missing" in validator.validate(fixed).errors


@pytest.mark.parametrize("initial_ids", [[], ["main1"], ["main1", "main2"]])
def test_short_selection_retries_complete_personalization(inputs, initial_ids):
    profile, analysis = inputs
    final_ids = ["main3", "main2", "main1"]
    provider = SequenceProvider([draft(initial_ids), draft(final_ids)])
    cv = CvAdapter(profile, provider).adapt(
        analysis, job_title="Data Scientist", job_company="Example"
    )
    assert len(provider.calls) == 2
    assert "too_few_projects:min=3" in provider.calls[1]["user"]
    assert "professional_summary" not in cv.model_dump()
    assert cv.selected_project_ids == final_ids


def test_short_second_selection_stops_before_letter(inputs):
    profile, analysis = inputs
    provider = SequenceProvider([draft(["main1"]), draft(["main1", "main2"])])
    with pytest.raises(LLMError, match="too_few_projects:min=3"):
        CvAdapter(profile, provider).adapt_application(
            analysis, job_title="Data Scientist", job_company="Example"
        )
    assert [call["purpose"] for call in provider.calls] == ["cv_adaptation", "cv_selection_correction"]


@pytest.mark.parametrize(
    "available_ids",
    [
        [],
        ["main1"],
        ["main1", "main2"],
        ["main1", "main2", "off"],
        ["main1", "special1", "special2", "off"],
    ],
)
def test_insufficient_selectable_catalogue_stops_before_llm(inputs, available_ids):
    profile, analysis = inputs
    profile.projects = [project for project in profile.projects if project.id in available_ids]
    provider = SequenceProvider([])
    with pytest.raises(LLMError):
        CvAdapter(profile, provider).adapt(
            analysis, job_title="Data Scientist", job_company="Example"
        )
    assert provider.calls == []


def test_unconfigured_specialized_project_cannot_complete_catalogue_capacity(inputs):
    profile, analysis = inputs
    profile.projects = [p for p in profile.projects if p.id in {"main1", "main2", "special1"}]
    profile.projects[-1].selection_policy.activation_conditions = [" "]
    provider = SequenceProvider([])
    with pytest.raises(LLMError, match="Insufficient eligible projects"):
        CvAdapter(profile, provider).adapt(
            analysis, job_title="Data Scientist", job_company="Example"
        )
    assert provider.calls == []


def test_historical_snapshot_without_policy_or_reasons_remains_readable(inputs):
    profile, _ = inputs
    project = Project.model_validate({"id": "old", "name": "Historical", "bullets": []})
    assert project.selection_policy.tier == "primary"
    legacy = draft(["main1", "main2", "main3", "special1"]).model_dump()
    legacy.pop("project_selection_reasons")
    legacy["professional_summary"] = "Previously generated summary."
    cv = assemble_cv(CVPersonalization.model_validate(legacy), profile)
    assert len(cv.selected_project_ids) == 4
    assert cv.project_selection_reasons == []
    assert cv.professional_summary == "Previously generated summary."


def test_first_structured_output_selects_evidence_without_generating_summary():
    schema = _to_strict_schema(CVSelection)
    fields = list(schema["properties"])
    assert fields.index("selected_project_ids") < fields.index("project_selection_reasons")
    assert "professional_summary" not in fields
    assert "project_selection_reasons" in schema["required"]
    assert "selected_certificate_ids" in schema["required"]


def test_auto_fix_never_silently_removes_selected_project(inputs):
    profile, _ = inputs
    cv = assemble_cv(draft(["off"]), profile)
    validator = CvValidator(profile)
    fixed, _ = validator.auto_fix(cv)
    assert fixed.selected_project_ids == cv.selected_project_ids
    assert fixed.project_selection_reasons == cv.project_selection_reasons
    assert not validator.validate(fixed).ok


def test_skill_role_contract_does_not_prune_selected_project(inputs):
    profile, analysis = inputs
    # Previously a personal-ID keyword table silently removed this audio project.
    cv = assemble_cv(draft(["proj_svc"]), profile)
    contracted, _ = apply_contract(
        cv,
        analysis=analysis,
        job_title="Data Scientist",
        allowed_skills_lower={s.lower() for s in profile.skills.allowed_skills},
    )
    assert contracted.selected_project_ids == cv.selected_project_ids
    assert contracted.project_selection_reasons == cv.project_selection_reasons
