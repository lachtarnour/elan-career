"""Tests for native desktop batch workflows and offer-table data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, get_ident
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject

from smartapply.database import Application, Job, JobStatus, ShortlistOrigin, session_scope
from smartapply.database.repository import (
    create_or_get_application,
    mark_analyzed,
    mark_archived,
    mark_filtered,
    set_analysis,
    set_score,
    set_shortlisted,
    upsert_job,
)
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.services import DesktopService
from smartapply.desktop.source_health import SourceHealth
from smartapply.jobsearch.workflow import APPLICATION_STATUSES


def _job(*, external_id: str, description: str = "Description") -> int:
    with session_scope() as session:
        job = upsert_job(
            session,
            external_id=external_id,
            title="Machine Learning Engineer",
            company="Acme",
            location="Paris",
            contract_type="CDI",
            description=description,
            source="manual",
        )
        session.flush()
        return job.id


def test_bridge_keeps_the_selected_top_k_for_shortlist_updates() -> None:
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._shortlist_top_k = 25
    bridge.service = SimpleNamespace(
        set_shortlist_top_k=lambda value: max(1, min(100, int(value))),
        update_shortlist=lambda **kwargs: kwargs,
    )
    captured: dict[str, int] = {}

    def capture_run(*args, **kwargs):
        captured.update(kwargs)

    bridge._run = capture_run

    bridge.updateShortlist(5)

    assert bridge.shortlistTopK == 5
    assert captured["top_k"] == 5


def test_bridge_passes_normalized_top_k_to_analysis() -> None:
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._analysis_top_k = 25
    bridge.service = SimpleNamespace(
        set_analysis_top_k=lambda value: max(1, min(100, int(value))),
        process_pending=lambda **kwargs: kwargs,
    )
    captured: dict[str, int] = {}

    def capture_run(*args, **kwargs):
        captured.update(kwargs)

    bridge._run = capture_run

    bridge.processPending(0)

    assert bridge.analysisTopK == 1
    assert captured["top_k"] == 1


def test_profile_directory_is_created_at_the_active_location(
    tmp_path,
    monkeypatch,
) -> None:
    profile_dir = tmp_path / "profile"
    monkeypatch.setenv("PROFILE_DIR", str(profile_dir))
    from smartapply.config import get_settings

    get_settings.cache_clear()
    try:
        resolved = DesktopService.ensure_profile_directory()
    finally:
        get_settings.cache_clear()

    assert resolved == profile_dir.resolve()
    assert resolved.is_dir()


def test_database_parent_directory_is_created_at_the_active_location(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "nested" / "data" / "smartapply.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    from smartapply.config import get_settings

    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    assert settings.database_url == f"sqlite:///{database_path}"
    assert database_path.parent.is_dir()


def test_job_rows_include_experience_and_application_state(isolated_db) -> None:
    job_id = _job(
        external_id="manual:table",
        description="Nous recherchons un profil avec 3+ ans d'expérience requis.",
    )
    with session_scope() as session:
        application = create_or_get_application(session, job_id)
        application_id = application.id

    rows = DesktopService().list_jobs()

    assert len(rows) == 1
    assert rows[0].experience == "3+ ans"
    assert rows[0].application_id == application_id
    assert rows[0].status == JobStatus.ANALYZED
    assert rows[0].analyzed is False
    assert rows[0].can_generate is False
    assert rows[0].can_send is True

    detail = DesktopService().get_job(job_id)
    assert detail is not None
    assert detail.application is not None
    assert detail.application.id == application_id
    assert detail.application.job_id == job_id


def test_dashboard_counts_confirmed_duplicate_submissions_once(isolated_db) -> None:
    root_id = _job(external_id="manual:dashboard-canonical")
    alias_id = _job(external_id="manual:dashboard-alias")
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        root = session.get(Job, root_id)
        alias = session.get(Job, alias_id)
        assert root is not None
        assert alias is not None
        alias.canonical_job_id = root_id
        alias.duplicate_review_status = "confirmed"
        alias.status = JobStatus.ARCHIVED
        alias.archived_at = now

        root_application = create_or_get_application(session, root_id)
        root_application.status = JobStatus.SENT
        root_application.form_submitted_at = now - timedelta(days=1)
        alias_application = create_or_get_application(session, alias_id)
        alias_application.status = JobStatus.SENT
        alias_application.form_submitted_at = now

    snapshot = DesktopService().dashboard()
    assert snapshot.sent == 1
    assert sum(point.count for point in snapshot.sent_by_day) == 1
    assert snapshot.sent_by_day[-2].count == 1
    assert snapshot.sent_by_day[-1].count == 0


def test_job_rows_expose_generation_capability_from_analysis_state(isolated_db) -> None:
    job_id = _job(external_id="manual:actions")

    row = DesktopService().list_jobs()[0]
    assert row.analyzed is False
    assert row.can_generate is False
    assert row.can_send is False

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.analyzed_at = datetime.now(timezone.utc)
        job.status = JobStatus.ANALYZED

    row = DesktopService().list_jobs()[0]
    assert row.analyzed is True
    assert row.can_generate is True
    assert row.can_send is False


def test_job_detail_keeps_profile_matches_and_review_points(isolated_db) -> None:
    job_id = _job(external_id="manual:feedback")
    with session_scope() as session:
        set_analysis(
            session,
            job_id,
            role_type="Data Scientist",
            seniority="junior",
            domain="IA appliquée",
            main_tasks=[],
            required_skills=["Python"],
            nice_to_have=[],
            match_reasons=["Python et machine learning correspondent au profil."],
            risks=["Vérifier le niveau d’expérience demandé."],
            cv_keywords_to_include=["Python"],
            raw_response={},
            model_used="test",
        )
        set_score(
            session,
            job_id,
            final_score=0.89,
            components={"reasons": ["search_context:origin=personalized_matches"]},
        )

    detail = DesktopService().get_job(job_id)

    assert detail is not None
    assert detail.match_reasons == ("Python et machine learning correspondent au profil.",)
    assert detail.risks == ("Vérifier le niveau d’expérience demandé.",)
    assert detail.archive_reasons == ()


def test_archived_job_detail_shows_only_human_readable_decisive_reason(isolated_db) -> None:
    job_id = _job(external_id="manual:archived-feedback")
    with session_scope() as session:
        set_score(
            session,
            job_id,
            components={
                "rejection_stage": "local_filter",
                "rejection_reasons": [
                    "search_context:origin=personalized_matches,chips=pages=150",
                    "remote_structured:hybrid",
                    "offer_language:en",
                    "experience_structured_welcometothejungle:5",
                    "experience_required_too_high:5+ years",
                ],
            },
        )
        mark_archived(session, job_id)

    detail = DesktopService().get_job(job_id)

    assert detail is not None
    assert detail.archive_reasons == ("Expérience demandée trop élevée : au moins 5 ans",)


def test_manual_archive_records_a_clear_reason(isolated_db) -> None:
    job_id = _job(external_id="manual:user-archive")
    with session_scope() as session:
        mark_analyzed(session, job_id)
        assert set_shortlisted(session, job_id, selected=True) is not None

    DesktopService().archive_job(job_id)
    detail = DesktopService().get_job(job_id)

    assert detail is not None
    assert detail.status == JobStatus.ARCHIVED
    assert detail.shortlisted is False
    assert detail.archive_reasons == ("Archivée manuellement",)
    with session_scope() as session:
        archived = session.get(Job, job_id)
        assert archived is not None
        assert archived.shortlisted_at is None
        assert archived.shortlist_origin is None


def test_archived_application_without_offer_marker_still_shows_archive_reason(isolated_db) -> None:
    job_id = _job(external_id="manual:legacy-archived-application")
    with session_scope() as session:
        application = create_or_get_application(session, job_id)
        application.status = JobStatus.ARCHIVED

    detail = DesktopService().get_job(job_id)

    assert detail is not None
    assert detail.status == JobStatus.ARCHIVED
    assert detail.archive_reasons == ("Offre écartée par les critères de sélection",)


def test_mark_job_sent_updates_linked_application_and_offer(isolated_db) -> None:
    job_id = _job(external_id="manual:user-sent")
    with session_scope() as session:
        application = create_or_get_application(session, job_id)
        application_id = application.id

    assert DesktopService().mark_job_sent(job_id) is True

    with session_scope() as session:
        job = session.get(Job, job_id)
        application = session.get(Application, application_id)
        assert job is not None and job.status == JobStatus.SENT
        assert application is not None and application.status == JobStatus.SENT
        assert application.form_submitted_at is not None


def test_mark_job_sent_without_application_is_rejected(isolated_db) -> None:
    job_id = _job(external_id="manual:user-sent-without-application")

    assert DesktopService().mark_job_sent(job_id) is False
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.status != JobStatus.SENT


@pytest.fixture
def restoration_llm():
    from smartapply.llm import MockLLMProvider
    from tests.test_pipeline import _register_llm_responses

    _register_llm_responses()
    yield
    MockLLMProvider.clear()


def test_rescue_jobs_prepares_documents_for_archived_selection(
    isolated_db, restoration_llm
) -> None:
    archived_id = _job(external_id="manual:archived")
    active_id = _job(external_id="manual:active")
    with session_scope() as session:
        mark_archived(session, archived_id)

    report = DesktopService().rescue_jobs([archived_id, archived_id, active_id, 999_999])

    assert report["requested"] == 3
    assert report["rescued"] == report["generated"] == 1
    assert report["skipped"] == 2
    assert report["failed"] == 0
    with session_scope() as session:
        rescued = session.get(Job, archived_id)
        assert rescued is not None
        assert rescued.status in (JobStatus.READY_FOR_FORM_SUBMISSION, JobStatus.QUALITY_REJECTED)
        assert rescued.shortlisted_at is None
        assert rescued.archived_at is None
        assert rescued.analyzed_at is not None
        assert rescued.score.components["manual_rescue"] is True
        assert rescued.application.id in report["application_ids"]
        assert rescued.application.cv_pdf_path
        assert {doc.doc_type for doc in rescued.application.documents} >= {
            "cv_json",
            "motivation_letter",
            "motivation_letter_pdf",
        }


@pytest.mark.parametrize("legacy_application_only", [False, True])
def test_rescue_regenerates_archived_dossier(
    isolated_db, restoration_llm, legacy_application_only
) -> None:
    job_id = _job(external_id="manual:restore-dossier")
    service = DesktopService()
    service.analyze_job(job_id)
    original = service.generate_application(job_id)
    with session_scope() as session:
        old_analysis_time = session.get(Job, job_id).analyzed_at
        if legacy_application_only:
            session.get(Application, original["application_id"]).status = JobStatus.ARCHIVED
    if not legacy_application_only:
        service.archive_application(original["application_id"])

    report = service.rescue_job(job_id)

    assert report["generated"] == 1
    assert report["failed"] == 0
    assert report["application_ids"] == [original["application_id"]]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.archived_at is None
        assert job.analyzed_at > old_analysis_time
        assert job.application.status != JobStatus.ARCHIVED
        assert len(session.query(Application).all()) == 1


def test_rescue_duplicate_resumes_stale_canonical_dossier(isolated_db, restoration_llm) -> None:
    root_id = _job(external_id="manual:restore-root")
    alias_id = _job(external_id="manual:restore-alias")
    with session_scope() as session:
        alias = session.get(Job, alias_id)
        alias.canonical_job_id = root_id
        alias.duplicate_review_status = "confirmed"
        mark_archived(session, alias_id)
        app = create_or_get_application(session, root_id)
        app.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        application_id = app.id

    detail = DesktopService().get_job(alias_id)
    assert detail.archive_reasons == (
        f"Doublon de l’offre « Machine Learning Engineer — Acme (n° {root_id}) »",
    )
    report = DesktopService().rescue_job(alias_id)

    assert report["generated"] == 1
    assert report["failed"] == 0
    assert report["job_ids"] == [root_id]
    assert report["application_ids"] == [application_id]
    with session_scope() as session:
        assert session.get(Job, alias_id).canonical_job_id == root_id
        assert session.get(Job, alias_id).archived_at is not None
        assert session.get(Job, root_id).analyzed_at is not None
        assert session.get(Application, application_id).cv_json
        assert session.query(Application).count() == 1


def test_rescue_recovers_archived_dossier_owned_by_an_alias(isolated_db, restoration_llm) -> None:
    owner_id = _job(external_id="manual:alias-owner")
    root_id = _job(external_id="manual:canonical-without-dossier")
    service = DesktopService()
    service.analyze_job(owner_id)
    original = service.generate_application(owner_id)
    service.archive_application(original["application_id"])
    with session_scope() as session:
        owner = session.get(Job, owner_id)
        owner.canonical_job_id = root_id
        owner.duplicate_review_status = "confirmed"

    report = service.rescue_job(owner_id)

    assert report["generated"] == 1
    assert report["failed"] == 0
    assert report["application_ids"] == [original["application_id"]]
    with session_scope() as session:
        assert session.get(Job, owner_id).canonical_job_id is None
        assert session.get(Job, owner_id).archived_at is None
        assert session.get(Job, root_id).canonical_job_id == owner_id
        assert session.query(Application).count() == 1


def test_rescue_keeps_existing_active_dossier_and_deduplicates_selection(
    isolated_db, restoration_llm
) -> None:
    root_id = _job(external_id="manual:existing-root")
    service = DesktopService()
    service.analyze_job(root_id)
    original = service.generate_application(root_id)
    aliases = [_job(external_id=f"manual:existing-alias-{index}") for index in range(2)]
    with session_scope() as session:
        for alias_id in aliases:
            alias = session.get(Job, alias_id)
            alias.canonical_job_id = root_id
            alias.duplicate_review_status = "confirmed"
            mark_archived(session, alias_id)

    report = service.rescue_jobs(aliases)

    assert report["existing"] == report["skipped"] == 1
    assert report["generated"] == report["failed"] == 0
    assert report["application_ids"] == [original["application_id"]]


def test_rescue_does_not_interrupt_active_generation(isolated_db) -> None:
    job_id = _job(external_id="manual:restore-reserved")
    with session_scope() as session:
        mark_archived(session, job_id)
        create_or_get_application(session, job_id)

    report = DesktopService().rescue_job(job_id)

    assert report["failed"] == 1
    assert report["generated"] == report["rescued"] == 0
    assert "déjà en cours" in report["errors"][0]["message"]
    with session_scope() as session:
        assert session.get(Job, job_id).archived_at is not None


def test_rescue_reports_analysis_failure_and_continues_batch(
    isolated_db, restoration_llm, monkeypatch
) -> None:
    first_id = _job(external_id="manual:restore-fails")
    second_id = _job(external_id="manual:restore-succeeds")
    with session_scope() as session:
        for job_id in (first_id, second_id):
            mark_archived(session, job_id)
    from smartapply.pipeline import Pipeline

    analyze = Pipeline.analyze_jobs

    def fail_first(self, ids):
        if ids == [first_id]:
            return SimpleNamespace(
                analyzed=0, already_analyzed=0, errors=[{"message": "API indisponible"}]
            )
        return analyze(self, ids)

    monkeypatch.setattr(Pipeline, "analyze_jobs", fail_first)
    report = DesktopService().rescue_jobs([first_id, second_id])

    assert report["generated"] == report["failed"] == 1
    assert "API indisponible" in report["errors"][0]["message"]
    with session_scope() as session:
        failed = session.get(Job, first_id)
        assert failed.archived_at is None
        assert failed.application is None
        assert failed.analyzed_at is None


def test_bridge_runs_single_rescue_in_background_and_opens_result() -> None:
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge.service = SimpleNamespace(rescue_job=lambda job_id: None)
    calls = []
    bridge._run = lambda *args, **kwargs: calls.append((args, kwargs))
    bridge.rescueJob(42)
    args, kwargs = calls[0]
    assert args[1:] == (bridge.service.rescue_job, 42)
    assert kwargs["on_success"] == bridge._bulk_rescue_done
    bridge._refresh_workflow = lambda: None
    bridge._set_activity = lambda *args: None
    opened = []
    bridge.openApplication = opened.append
    toasts = []
    bridge.toastRequested.connect(lambda *args: toasts.append(args))
    bridge._bulk_rescue_done(
        {
            "requested": 1,
            "generated": 1,
            "application_ids": [123],
            "warnings": [{"job_id": 42, "message": "PDF indisponible"}],
        }
    )
    assert opened == [123]
    assert toasts[0][2] == "warning"
    assert "PDF indisponible" in toasts[0][1]


def test_manual_top_selection_persists_across_service_instances(isolated_db) -> None:
    job_id = _job(external_id="manual:persistent-shortlist")
    with session_scope() as session:
        mark_analyzed(session, job_id)

    assert DesktopService().set_job_shortlisted(job_id, selected=True) is True

    reloaded_service = DesktopService()
    detail = reloaded_service.get_job(job_id)
    summary = reloaded_service.shortlist_summary()
    top_rows = reloaded_service.list_jobs(status=JobStatus.SHORTLISTED)
    assert detail is not None and detail.shortlisted is True
    assert detail.status_label == "Top sélection"
    assert summary.total == 1
    assert summary.ready_to_generate == 1
    assert [row.id for row in top_rows] == [job_id]
    with session_scope() as session:
        persisted = session.get(Job, job_id)
        assert persisted is not None
        assert persisted.shortlist_origin == ShortlistOrigin.MANUAL

    assert reloaded_service.set_job_shortlisted(job_id, selected=False) is True
    final_detail = DesktopService().get_job(job_id)
    assert final_detail is not None
    assert final_detail.shortlisted is False
    assert final_detail.status == JobStatus.ANALYZED
    assert final_detail.archive_reasons == ()


def test_manual_top_selection_analyzes_unanalyzed_offer_first(isolated_db, monkeypatch) -> None:
    job_id = _job(external_id="manual:auto-analysis-before-shortlist")
    analyzed_calls: list[list[int]] = []

    class FakePipeline:
        def analyze_jobs(self, job_ids):
            analyzed_calls.append(list(job_ids))
            with session_scope() as session:
                mark_analyzed(session, job_id)
            return SimpleNamespace(analyzed=1, already_analyzed=0, errors=[])

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    assert DesktopService().set_job_shortlisted(job_id, selected=True) is True
    detail = DesktopService().get_job(job_id)
    assert detail is not None
    assert analyzed_calls == [[job_id]]
    assert detail.analyzed is True
    assert detail.status == JobStatus.SHORTLISTED


def test_removing_analyzed_offer_from_top_selection_keeps_analyzed_status(isolated_db) -> None:
    job_id = _job(external_id="manual:analyzed-shortlist-removal")
    service = DesktopService()
    with session_scope() as session:
        mark_analyzed(session, job_id)

    assert service.set_job_shortlisted(job_id, selected=True) is True

    assert service.set_job_shortlisted(job_id, selected=False) is True
    final_detail = DesktopService().get_job(job_id)
    assert final_detail is not None
    assert final_detail.status == JobStatus.ANALYZED
    assert final_detail.analyzed is True


def test_new_filter_only_includes_filtered_offers_waiting_for_analysis(isolated_db) -> None:
    pending_id = _job(external_id="manual:pending-filter")
    shortlisted_id = _job(external_id="manual:shortlisted-filter")
    service = DesktopService()
    with session_scope() as session:
        mark_filtered(session, pending_id)
        mark_filtered(session, shortlisted_id)
        mark_analyzed(session, shortlisted_id)
        set_shortlisted(session, shortlisted_id, selected=True)

    new_rows = service.list_jobs(status=JobStatus.SCRAPED)

    assert [row.id for row in new_rows] == [pending_id]
    assert all(row.status == JobStatus.SCRAPED for row in new_rows)


def test_shortlist_filter_excludes_generated_applications(isolated_db) -> None:
    shortlisted_id = _job(external_id="manual:shortlist-without-app")
    generated_id = _job(external_id="manual:shortlist-with-app")
    with session_scope() as session:
        for job_id in (shortlisted_id, generated_id):
            mark_analyzed(session, job_id)
            set_shortlisted(session, job_id, selected=True)
        create_or_get_application(session, generated_id)

    rows = DesktopService().list_jobs(status=JobStatus.SHORTLISTED)

    assert [row.id for row in rows] == [shortlisted_id]


def test_shortlist_summary_ignores_unanalyzed_legacy_rows(isolated_db) -> None:
    valid_id = _job(external_id="manual:valid-summary-shortlist")
    stale_id = _job(external_id="manual:stale-summary-shortlist")
    with session_scope() as session:
        mark_analyzed(session, valid_id)
        assert set_shortlisted(session, valid_id, selected=True) is not None
        stale = session.get(Job, stale_id)
        assert stale is not None
        stale.shortlisted_at = datetime.now(timezone.utc)
        stale.status = JobStatus.SHORTLISTED

    service = DesktopService()

    assert service.shortlist_summary().total == 1
    assert service.shortlisted_generation_ids() == [valid_id]


def test_job_statuses_hide_internal_filtered_state(isolated_db) -> None:
    statuses = DesktopService.job_statuses()

    assert JobStatus.FILTERED not in {value for value, _label in statuses}
    assert [value for value, _label in statuses] == [
        JobStatus.DUPLICATE_REVIEW,
        JobStatus.SCRAPED,
        JobStatus.ANALYZED,
        JobStatus.SHORTLISTED,
        JobStatus.READY_FOR_FORM_SUBMISSION,
        JobStatus.SENT,
        JobStatus.ARCHIVED,
    ]


def test_job_status_filters_follow_application_tracking_state(isolated_db) -> None:
    job_ids: dict[str, int] = {}
    with session_scope() as session:
        for index, status in enumerate(APPLICATION_STATUSES):
            job = upsert_job(
                session,
                external_id=f"manual:application-status:{index}",
                title="Machine Learning Engineer",
                company="Acme",
                location="Paris",
                contract_type="CDI",
                description="Description",
                source="manual",
            )
            application = create_or_get_application(session, job.id)
            application.status = status
            job.status = status
            if status == JobStatus.ARCHIVED:
                mark_archived(session, job.id)
            job_ids[status] = job.id

    service = DesktopService()
    for status, job_id in job_ids.items():
        rows = service.list_jobs(status=status)
        assert [row.id for row in rows] == [job_id]


def test_shortlist_top_k_preference_is_managed_and_persisted(isolated_db) -> None:
    service = DesktopService()

    assert service.set_shortlist_top_k(12) == 12
    assert DesktopService().shortlist_top_k() == 12
    assert service.set_shortlist_top_k(500) == 100
    assert DesktopService().shortlist_top_k() == 100


def test_analysis_and_shortlist_top_k_preferences_are_independent(isolated_db) -> None:
    service = DesktopService()

    assert service.set_analysis_top_k(7) == 7
    assert service.set_shortlist_top_k(13) == 13

    assert DesktopService().analysis_top_k() == 7
    assert DesktopService().shortlist_top_k() == 13


def test_generate_top_selection_ignores_non_selected_jobs(isolated_db, monkeypatch) -> None:
    first_id = _job(external_id="manual:top-first")
    second_id = _job(external_id="manual:top-second")
    outside_id = _job(external_id="manual:not-in-top")
    with session_scope() as session:
        mark_analyzed(session, first_id)
        mark_analyzed(session, second_id)
        set_shortlisted(session, first_id, selected=True)
        set_shortlisted(session, second_id, selected=True)

    analyzed_calls: list[int] = []
    generated_calls: list[int] = []

    class FakePipeline:
        def analyze_jobs(self, job_ids):
            analyzed_calls.extend(job_ids)
            return SimpleNamespace(analyzed=1, already_analyzed=0)

        def apply_to(self, job_id):
            generated_calls.append(job_id)
            return SimpleNamespace(application_id=job_id + 1000)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    report = DesktopService().generate_shortlisted_applications()

    assert report["requested"] == 2
    assert report["generated"] == 2
    assert analyzed_calls == []
    assert generated_calls == [first_id, second_id]
    assert outside_id not in generated_calls


def test_generate_applications_analyzes_new_rows_and_skips_ineligible(
    isolated_db,
    monkeypatch,
) -> None:
    analyzed_id = _job(external_id="manual:analyzed")
    new_id = _job(external_id="manual:new")
    archived_id = _job(external_id="manual:archived-for-generation")
    existing_id = _job(external_id="manual:existing")
    with session_scope() as session:
        analyzed = session.get(Job, analyzed_id)
        assert analyzed is not None
        analyzed.analyzed_at = datetime.now(timezone.utc)
        analyzed.status = JobStatus.ANALYZED
        mark_archived(session, archived_id)
        create_or_get_application(session, existing_id)

    analyzed_calls: list[int] = []
    generated_calls: list[int] = []

    class FakePipeline:
        def analyze_jobs(self, job_ids):
            analyzed_calls.extend(job_ids)
            return SimpleNamespace(analyzed=1, already_analyzed=0)

        def apply_to(self, job_id):
            if job_id == existing_id:
                from smartapply.pipeline import ApplicationAlreadyExistsError

                raise ApplicationAlreadyExistsError(job_id)
            generated_calls.append(job_id)
            return SimpleNamespace(application_id=job_id + 1000)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    report = DesktopService().generate_applications([analyzed_id, new_id, archived_id, existing_id])

    assert report["generated"] == 2
    assert report["skipped"] == 2
    assert report["failed"] == 0
    assert analyzed_calls == [new_id]
    assert generated_calls == [analyzed_id, new_id]


def test_generate_applications_reports_analysis_failure_details(
    isolated_db,
    monkeypatch,
) -> None:
    job_id = _job(external_id="manual:analysis-failure")

    class FakePipeline:
        def analyze_jobs(self, job_ids):
            return SimpleNamespace(
                analyzed=0,
                already_analyzed=0,
                errors=[{"job_id": job_ids[0], "message": "quota indisponible"}],
            )

        def apply_to(self, job_id):  # noqa: ARG002
            raise AssertionError("generation must not run after failed analysis")

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    report = DesktopService().generate_applications([job_id])

    assert report["generated"] == 0
    assert report["skipped"] == 0
    assert report["failed"] == 1
    assert report["errors"] == [{"job_id": job_id, "message": "Analyse : quota indisponible"}]


def test_generate_applications_returns_document_warnings(
    isolated_db,
    monkeypatch,
) -> None:
    job_id = _job(external_id="manual:generation-warning")
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.analyzed_at = datetime.now(timezone.utc)
        job.status = JobStatus.ANALYZED

    class FakePipeline:
        def apply_to(self, received_job_id):
            assert received_job_id == job_id
            return SimpleNamespace(
                application_id=73,
                validation_warnings=["cv_pdf_not_generated"],
                validation_errors=[],
            )

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    report = DesktopService().generate_applications([job_id])

    assert report["generated"] == 1
    assert report["warnings"] == [
        {
            "job_id": job_id,
            "title": "Machine Learning Engineer",
            "company": "Acme",
            "message": "cv_pdf_not_generated",
        }
    ]


def test_manual_generation_returns_document_warnings(monkeypatch) -> None:
    from smartapply.pipeline import ApplyReport

    class FakePipeline:
        def ingest_text(self, *args, **kwargs):  # noqa: ARG002
            return SimpleNamespace(job_ids=[42])

        def analyze_jobs(self, job_ids):
            assert job_ids == [42]
            return SimpleNamespace(analyzed=1, already_analyzed=0)

        def apply_to(self, job_id, *, form_url):
            assert job_id == 42
            assert form_url == "https://example.test/apply"
            return ApplyReport(
                job_id=job_id,
                application_id=7,
                validation_warnings=["letter_pdf_not_generated"],
            )

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    report = DesktopService().create_manual_application(
        title="Data Scientist",
        company="Acme",
        location="Paris",
        description="Une description suffisamment complète.",
        application_url="https://example.test/apply",
    )

    assert report["application_id"] == 7
    assert report["validation_warnings"] == ["letter_pdf_not_generated"]


def test_diagnostics_use_verified_source_health(monkeypatch) -> None:
    verified = {
        "serpapi": SourceHealth(True, False, "unavailable", "Quota épuisé."),
        "francetravail": SourceHealth(False, False, "unconfigured", "Absent."),
        "linkedin": SourceHealth(True, True, "available", "Jeton valide."),
        "welcometothejungle": SourceHealth(
            True,
            False,
            "unavailable",
            "Session expirée.",
        ),
    }
    monkeypatch.setattr(
        "smartapply.desktop.services.check_source_health",
        lambda settings: verified,  # noqa: ARG005
    )

    diagnostics = DesktopService().diagnostics(check_sources=True)

    assert diagnostics.source_health == verified
    assert diagnostics.source_ready == {
        "serpapi": False,
        "francetravail": False,
        "linkedin": True,
        "welcometothejungle": False,
    }


def test_search_requires_the_location_selected_in_the_app(isolated_db) -> None:
    with pytest.raises(ValueError, match="localisation"):
        DesktopService().search_jobs(
            query="Data Scientist",
            location="   ",
            sources=["serpapi"],
            max_results=10,
            date_posted="week",
        )


def test_generate_application_lets_pipeline_validate_existing_reservation(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.pipeline import ApplicationAlreadyExistsError

    job_id = _job(external_id="manual:single-existing")
    with session_scope() as session:
        app = create_or_get_application(session, job_id)
        application_id = app.id

    class FakePipeline:
        def apply_to(self, received_job_id):
            assert received_job_id == job_id
            raise ApplicationAlreadyExistsError(job_id, application_id)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    with pytest.raises(ApplicationAlreadyExistsError) as exc_info:
        DesktopService().generate_application(job_id)

    assert exc_info.value.application_id == application_id


def test_generate_application_returns_existing_dossier_for_stale_screen(
    isolated_db, monkeypatch
) -> None:
    from smartapply.pipeline import ApplicationAlreadyExistsError

    job_id = _job(external_id="manual:existing-documents")
    with session_scope() as session:
        app = create_or_get_application(session, job_id)
        app.cv_docx_path = "/documents/CV.docx"
        application_id = app.id

    class FakePipeline:
        def apply_to(self, received_job_id):
            assert received_job_id == job_id
            raise ApplicationAlreadyExistsError(job_id, application_id)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)
    assert DesktopService().generate_application(job_id) == {
        "job_id": job_id,
        "application_id": application_id,
        "existing": True,
    }


def test_bridge_opens_existing_application_instead_of_reporting_creation() -> None:
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._refresh_workflow = lambda: None
    bridge._set_activity = lambda *args: None
    opened = []
    notifications = []
    bridge.openApplication = opened.append
    bridge.toastRequested.connect(lambda *args: notifications.append(args))

    bridge._generation_done({"job_id": 705, "application_id": 127, "existing": True})

    assert opened == [127]
    assert notifications[0][0] == "Candidature existante"
    assert notifications[0][2] == "neutral"


def test_opening_archived_dossier_switches_filter_and_keeps_requested_selection() -> None:
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._current_job = {"id": 1115}
    bridge._selected_job_id = 1115
    bridge._job_detail_loading = False
    bridge.service = SimpleNamespace(get_job=lambda job_id: None)
    bridge._start_read = lambda *args, **kwargs: None
    routes = []
    bridge.navigationRequested.connect(lambda *args: routes.append(args))

    bridge._application_opened_in_jobs(SimpleNamespace(job_id=705, status=JobStatus.ARCHIVED))
    assert routes == [("jobs?status=archived", 705)]
    assert bridge._selected_job_id == 705

    # The list may finish loading before the requested dossier detail.
    selected = []
    bridge.selectJob = selected.append
    bridge._jobs_loaded([{"id": 2}, {"id": 705}])
    assert selected == []


def test_search_collects_sources_in_parallel_then_persists_serially(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.pipeline.ingest import IngestCollection, IngestReport

    rendezvous = Barrier(2, timeout=2)
    collection_threads: list[int] = []
    persistence_calls: list[tuple[str, int]] = []
    phase_calls: list[tuple[str, list[int]]] = []
    caller_thread = get_ident()

    class FakePipeline:
        def collect_source(self, source, *args, **kwargs):  # noqa: ARG002
            collection_threads.append(get_ident())
            rendezvous.wait()
            return IngestCollection(source=source, raw_jobs=[])

        def persist_collection(self, collection):
            persistence_calls.append((collection.source, get_ident()))
            phase_calls.append(("persist", []))
            return IngestReport(
                source=collection.source,
                fetched=1,
                persisted=1,
                job_ids=[len(phase_calls)],
            )

        def filter_pending(self, *, job_ids=None):
            phase_calls.append(("filter", list(job_ids or [])))

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    result = DesktopService().search_jobs(
        query="Data Scientist",
        location="Paris",
        sources=["serpapi", "linkedin"],
        max_results=10,
        date_posted="week",
    )

    assert len(set(collection_threads)) == 2
    assert persistence_calls == [
        ("serpapi", caller_thread),
        ("linkedin", caller_thread),
    ]
    assert phase_calls == [("persist", []), ("persist", []), ("filter", [1, 2])]
    assert not result.errors


def test_search_propagates_cooperative_cancellation(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.pipeline.ingest import IngestCollection, IngestReport

    class FakePipeline:
        def collect_source(self, source, *args, stop_requested=None, **kwargs):  # noqa: ARG002
            return IngestCollection(
                source=source,
                raw_jobs=[],
                cancelled=bool(stop_requested and stop_requested()),
            )

        def persist_collection(self, collection):
            return IngestReport(
                source=collection.source,
                fetched=0,
                persisted=0,
                cancelled=collection.cancelled,
            )

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    result = DesktopService().search_jobs(
        query="Data Scientist",
        location="Paris",
        sources=["serpapi", "linkedin"],
        max_results=10,
        date_posted="week",
        stop_requested=lambda: True,
    )

    assert result.cancelled is True
    assert len(result.reports) == 2


def test_search_preserves_source_warnings_for_the_interface(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.pipeline.ingest import IngestCollection, IngestReport

    class FakePipeline:
        def collect_source(self, source, *args, **kwargs):  # noqa: ARG002
            return IngestCollection(
                source=source,
                raw_jobs=[],
                warnings=["WTTJ : une fiche était illisible."],
            )

        def persist_collection(self, collection):
            return IngestReport(
                source=collection.source,
                fetched=0,
                persisted=0,
                warnings=collection.warnings,
            )

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    result = DesktopService().search_jobs(
        query="Data Scientist",
        location="France",
        sources=["welcometothejungle"],
        max_results=10,
        date_posted="week",
    )

    assert result.reports[0]["warnings"] == ["WTTJ : une fiche était illisible."]


def test_application_output_directory_is_scoped_to_selected_application(
    isolated_db,
) -> None:
    first_job_id = _job(external_id="manual:first-folder")
    second_job_id = _job(external_id="manual:second-folder")
    with session_scope() as session:
        first = create_or_get_application(session, first_job_id)
        second = create_or_get_application(session, second_job_id)
        session.flush()
        first_id = first.id
        second_id = second.id

    service = DesktopService()

    assert (
        service.application_output_directory(first_id)
        == (isolated_db / "output" / str(first_id)).resolve()
    )
    assert (
        service.application_output_directory(second_id)
        == (isolated_db / "output" / str(second_id)).resolve()
    )
    assert service.application_output_directory(999_999) is None


def test_application_output_directory_uses_persisted_document_location(
    isolated_db,
) -> None:
    job_id = _job(external_id="manual:persisted-folder")
    with session_scope() as session:
        application = create_or_get_application(session, job_id)
        application.cv_pdf_path = str(isolated_db / "output" / "custom-name" / "CV.pdf")
        session.flush()
        application_id = application.id

    assert (
        DesktopService().application_output_directory(application_id)
        == (isolated_db / "output" / "custom-name").resolve()
    )
