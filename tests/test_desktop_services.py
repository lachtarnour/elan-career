"""Tests for native desktop batch workflows and offer-table data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, get_ident
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject

from smartapply.database import Application, Job, JobStatus, ShortlistOrigin, session_scope
from smartapply.database.fit_ranking import current_policy_fingerprint
from smartapply.database.repository import (
    create_or_get_application,
    mark_analyzed,
    mark_archived,
    mark_filtered,
    set_score,
    set_shortlisted,
    upsert_job,
)
from smartapply.database.repository import (
    set_analysis as repository_set_analysis,
)
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.services import DesktopService
from smartapply.desktop.source_health import SourceHealth
from smartapply.jobsearch.workflow import APPLICATION_STATUSES
from smartapply.pipeline.reports import AnalyzeReport


def set_analysis(session, job_id, **fields):
    """These workflow fixtures represent scores from the active policy."""
    fields.setdefault("fit_diagnostics", {"policy_fingerprint": current_policy_fingerprint()})
    return repository_set_analysis(session, job_id, **fields)


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


def _prepare_analyzed_offer(session, job_id: int) -> None:
    mark_filtered(session, job_id)
    set_score(
        session,
        job_id,
        components={
            "filter_disposition": "pending_relevance",
            "hard_reject": {
                "ruleset_version": "hard-reject-v2",
                "rejected": False,
                "gates": [],
            },
        },
    )
    set_analysis(session, job_id, fit_score=0.75)
    mark_analyzed(session, job_id)


def test_job_detail_exposes_classification_without_changing_the_record(isolated_db) -> None:
    job_id = _job(external_id="classification-detail")
    components = {
        "filter_disposition": "pending_relevance",
        "hard_reject": {
            "rejected": False,
            "gates": [{"gate": "contract", "status": "unresolved"}],
        },
    }
    with session_scope() as session:
        mark_filtered(session, job_id)
        set_score(session, job_id, components=components)

    detail = DesktopService().get_job(job_id)

    assert detail is not None
    assert detail.has_analysis is False
    assert detail.main_tasks == detail.required_skills == detail.nice_to_have == ()
    assert "pertinence" in detail.classification_summary
    assert detail.classification_reasons
    assert "contrat" in " ".join(detail.classification_reasons).lower()
    with session_scope() as session:
        assert session.get(Job, job_id).score.components == components


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


def test_selected_analysis_is_scoped_and_does_not_persist_a_shortlist(isolated_db, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakePipeline:
        def prepare_analysis(self, **kwargs):
            calls.append(("prepare", kwargs))

        def analyze_jobs(self, job_ids):
            calls.append(("analyze", list(job_ids)))
            return AnalyzeReport(
                requested=len(job_ids),
                already_analyzed=0,
                analyzed=len(job_ids),
                skipped_missing=0,
            )

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    report = DesktopService().analyze_selected_jobs([8, 3, 8, 0])

    assert calls == [
        (
            "prepare",
            {"job_ids": [8, 3]},
        ),
        ("analyze", [8, 3]),
    ]
    assert report == {
        "requested": 2,
        "already_analyzed": 0,
        "analyzed": 2,
        "skipped_missing": 0,
        "errors": [],
        "cancelled": False,
        "screened_out": 0,
    }


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


def test_profile_preview_uses_current_experience_and_project_records(monkeypatch) -> None:
    from smartapply.profile import load_profile

    profile = load_profile()
    experience = profile.experiences[0].model_copy(
        update={"title": "Poste actualisé", "company": "Entreprise actualisée",
                "start_date": "2023-04", "end_date": "Present"}
    )
    project = profile.projects[0].model_copy(update={"name": "Projet actualisé"})
    current = profile.model_copy(update={"experiences": [experience], "projects": [project]})
    monkeypatch.setattr("smartapply.desktop.services.get_profile", lambda: current)

    snapshot = DesktopService().profile()

    assert snapshot.experiences == 1 and snapshot.projects == 1
    assert [(item.id, item.title, item.company, item.start_date, item.end_date)
            for item in snapshot.experience_items] == [
        (experience.id, "Poste actualisé", "Entreprise actualisée", "2023-04", "Present")
    ]
    assert [(item.id, item.title) for item in snapshot.project_items] == [
        (project.id, "Projet actualisé")
    ]


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
    assert rows[0].status == JobStatus.SCRAPED
    assert not rows[0].analyzed  # An empty reservation is not a completed analysis.
    assert rows[0].analyzed is False
    assert rows[0].can_generate is False
    assert rows[0].can_send is False

    detail = DesktopService().get_job(job_id)
    assert detail is not None
    assert detail.application is not None
    assert detail.application.id == application_id
    assert detail.application.job_id == job_id


def test_new_offer_rows_expose_and_sort_by_publication_date(isolated_db) -> None:
    older_id = _job(external_id="manual:older-publication")
    newer_id = _job(external_id="manual:newer-publication")
    with session_scope() as session:
        older = session.get(Job, older_id)
        newer = session.get(Job, newer_id)
        assert older is not None
        assert newer is not None
        older.published_date = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
        newer.published_date = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
        mark_filtered(session, older_id)
        mark_filtered(session, newer_id)

    rows = DesktopService().list_jobs(
        status=JobStatus.SCRAPED,
        sort_key="published_date",
        sort_ascending=False,
    )

    assert [row.id for row in rows] == [newer_id, older_id]
    assert [row.published_date for row in rows] == ["01/10/2026", "28/09/2026"]


def test_archived_offer_date_is_exposed_and_sorted_across_pages(isolated_db) -> None:
    ids = [_job(external_id=f"archive-date-{index}") for index in range(3)]
    with session_scope() as session:
        for job_id in ids:
            mark_archived(session, job_id)
        session.get(Job, ids[0]).archived_at = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        session.get(Job, ids[1]).archived_at = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        session.get(Job, ids[2]).archived_at = None

    service = DesktopService()
    rows = service.list_jobs(status=JobStatus.ARCHIVED, sort_key="archived_at")
    assert [row.id for row in rows] == [ids[1], ids[0], ids[2]]
    assert [row.archived_at for row in rows] == ["05/10/2026", "02/10/2026", "—"]
    page = service.list_jobs_page(
        status=JobStatus.ARCHIVED, sort_key="archived_at", page=1, page_size=1,
    )
    assert page["total"] == 3 and page["rows"][0].id == ids[0]
    assert service.get_job(ids[1]).archived_at == "05/10/2026"


def test_analysis_dates_use_completion_and_sort_across_pages(isolated_db) -> None:
    ids = [_job(external_id=f"analysis-date-{index}") for index in range(3)]
    with session_scope() as session:
        for index, job_id in enumerate(ids):
            _prepare_analyzed_offer(session, job_id)
            session.get(Job, job_id).analyzed_at = datetime(
                2026, 10, index + 1, 12, tzinfo=timezone.utc,
            )
    service = DesktopService()
    rows = service.list_jobs(status=JobStatus.ANALYZED, sort_key="analyzed_at")
    assert [row.id for row in rows] == ids[::-1]
    assert [row.analyzed_at for row in rows] == ["03/10/2026", "02/10/2026", "01/10/2026"]
    page = service.list_jobs_page(
        status=JobStatus.ANALYZED, sort_key="analyzed_at", page=1, page_size=1,
    )
    assert page["total"] == 3 and page["rows"][0].id == ids[1]
    assert service.get_job(ids[2]).analyzed_at == "03/10/2026"


def test_preparation_dates_keep_document_evidence_after_note_edits(isolated_db) -> None:
    from smartapply.database.models import GeneratedDocument
    from smartapply.database.repository import update_application_tracking

    historical_id = _job(external_id="historical-preparation-date")
    current_id = _job(external_id="current-preparation-date")
    unknown_id = _job(external_id="unknown-preparation-date")
    with session_scope() as session:
        for job_id in (historical_id, current_id, unknown_id):
            _prepare_analyzed_offer(session, job_id)
            application = create_or_get_application(session, job_id)
            application.status = JobStatus.READY_FOR_FORM_SUBMISSION
            application.cv_docx_path = f"/tmp/{application.id}/cv.docx"
            application.created_at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
            if job_id == current_id:
                application.ready_at = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
            if job_id == historical_id:
                historical_app_id = application.id
                session.add(GeneratedDocument(
                    application_id=application.id, doc_type="document_review",
                    created_at=datetime(2026, 10, 2, 12, tzinfo=timezone.utc),
                ))
        session.flush()
        update_application_tracking(session, historical_app_id, notes="New note")

    service = DesktopService()
    rows = service.list_jobs(status=JobStatus.READY_FOR_FORM_SUBMISSION, sort_key="ready_at")
    assert [row.id for row in rows] == [current_id, historical_id, unknown_id]
    assert [row.ready_at for row in rows] == ["04/10/2026", "02/10/2026", "—"]
    page = service.list_jobs_page(
        status=JobStatus.READY_FOR_FORM_SUBMISSION, sort_key="ready_at", page_size=1,
    )
    assert page["total"] == 3 and page["rows"][0].id == current_id
    assert service.get_job(historical_id).ready_at == "02/10/2026"
    operation_rows = service.list_jobs(
        status=JobStatus.READY_FOR_FORM_SUBMISSION, sort_key="operation_date",
    )
    assert [row.id for row in operation_rows] == [current_id, historical_id, unknown_id]
    assert [row.operation_date for row in operation_rows] == [
        "04/10/2026", "02/10/2026", "—",
    ]


def test_operation_date_groups_day_then_ai_score_across_pages(isolated_db) -> None:
    ids = [_job(external_id=f"operation-sort-{index}") for index in range(3)]
    with session_scope() as session:
        for job_id, day, hour, score in zip(
            ids, [5, 5, 6], [18, 9, 9], [0.4, 0.9, 0.2], strict=True,
        ):
            _prepare_analyzed_offer(session, job_id)
            set_analysis(session, job_id, fit_score=score)
            session.get(Job, job_id).analyzed_at = datetime(
                2026, 10, day, hour, tzinfo=timezone.utc,
            )

    service = DesktopService()
    rows = service.list_jobs(status=JobStatus.ANALYZED, sort_key="operation_date")
    assert [row.id for row in rows] == [ids[2], ids[1], ids[0]]
    assert [row.operation_date for row in rows] == [
        "06/10/2026", "05/10/2026", "05/10/2026",
    ]
    page = service.list_jobs_page(
        status=JobStatus.ANALYZED, sort_key="operation_date", page=1, page_size=1,
    )
    assert page["total"] == 3 and page["rows"][0].id == ids[1]
    ascending = service.list_jobs(
        status=JobStatus.ANALYZED, sort_key="operation_date", sort_ascending=True,
    )
    assert [row.id for row in ascending] == [ids[1], ids[0], ids[2]]


def test_sent_operation_date_uses_submission_time_only(isolated_db) -> None:
    sent_id = _job(external_id="sent-operation-date")
    missing_id = _job(external_id="sent-without-submission-date")
    with session_scope() as session:
        for job_id in (sent_id, missing_id):
            application = create_or_get_application(session, job_id)
            application.status = JobStatus.SENT
            application.updated_at = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
            if job_id == sent_id:
                application.form_submitted_at = datetime(
                    2026, 10, 4, 12, tzinfo=timezone.utc,
                )

    rows = DesktopService().list_jobs(status=JobStatus.SENT, sort_key="operation_date")
    assert [row.id for row in rows] == [sent_id, missing_id]
    assert [row.operation_date for row in rows] == ["04/10/2026", "—"]


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
    assert DesktopService().get_job(job_id).has_analysis is False


def test_job_detail_keeps_profile_matches_and_review_points(isolated_db) -> None:
    job_id = _job(external_id="manual:feedback")
    with session_scope() as session:
        set_analysis(
            session,
            job_id,
            role_type="Data Scientist",
            seniority="junior",
            domain="IA appliquée",
            main_tasks=["Développer des modèles de classification."],
            required_skills=["Python"],
            nice_to_have=["SQL"],
            match_reasons=["Python et machine learning correspondent au profil."],
            risks=["Vérifier le niveau d’expérience demandé."],
            cv_keywords_to_include=["Python"],
            raw_response={},
            model_used="test",
            fit_diagnostics={
                "evaluation": {"dimensions": {"missions": {"findings": [
                    {"text": "La pratique du machine learning appuie les missions."},
                    {"text": "La pratique du machine learning appuie les missions."},
                    {"text": ""},
                ]}, "essentials": {"findings": [
                    {"text": "La pratique de SQL est déclarée.", "support": "declared_only"},
                    {"text": "Une pratique de déploiement manque.", "support": "not_documented"},
                ]}}},
                "used_levels": {"missions": 3},
                "used_decisions": {"missions": "credible_fit"},
            },
        )
        set_score(
            session,
            job_id,
            components={"reasons": ["search_context:origin=personalized_matches"]},
        )

    detail = DesktopService().get_job(job_id)

    assert detail is not None
    assert detail.has_analysis is True
    assert (detail.role_type, detail.seniority, detail.domain) == (
        "Data Scientist", "junior", "IA appliquée"
    )
    assert detail.main_tasks == ("Développer des modèles de classification.",)
    assert detail.required_skills == ("Python",)
    assert detail.nice_to_have == ("SQL",)
    assert detail.fit_axes[0]["findings"] == [
        "La pratique du machine learning appuie les missions."
    ]
    assert detail.fit_axes[1]["findings"] == [
        "Déclaré : La pratique de SQL est déclarée.",
        "Non documenté : Une pratique de déploiement manque.",
    ]
    assert detail.fit_axes[2]["findings"] == []
    assert detail.match_reasons == ("Python et machine learning correspondent au profil.",)
    assert detail.risks == ("Vérifier le niveau d’expérience demandé.",)
    assert detail.archive_reasons == ()


def test_application_detail_exposes_short_review_points_without_changing_audit(
    isolated_db,
) -> None:
    job_id = _job(external_id="manual:document-review-presentation")
    raw = [
        "text_review_corrected: correction vérifiée",
        "text_review:structure:motivation_letter_body: " + "Private letter passage " * 200,
        "letter_too_long:252",
        "validation_error:text_review_structural_error:letter_too_long:252",
        "validation_error:letter_too_long:252",
        "cv_title_not_offer_anchored",
    ]
    with session_scope() as session:
        application = create_or_get_application(session, job_id)
        application.status = JobStatus.READY_FOR_FORM_SUBMISSION
        application.validation_warnings = raw

    detail = DesktopService().get_job(job_id)

    assert detail is not None and detail.application is not None
    assert detail.application.warnings == tuple(raw)
    assert detail.application.review_corrected is True
    assert [point["key"] for point in detail.application.review_points] == [
        "structure:motivation_letter_body", "letter_length", "cv_title_alignment",
    ]
    assert "252 mots" in detail.application.review_points[1]["detail"]
    assert "Private letter passage" not in str(detail.application.review_points)


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
        _prepare_analyzed_offer(session, job_id)
        assert set_shortlisted(session, job_id, selected=True) is not None

    DesktopService().archive_job(job_id)
    detail = DesktopService().get_job(job_id)

    assert detail is not None
    assert detail.status == JobStatus.ARCHIVED
    assert detail.archive_sub_label == "Archivage manuel"
    assert detail.shortlisted is False
    assert detail.archive_reasons == ("Archivée manuellement",)
    with session_scope() as session:
        archived = session.get(Job, job_id)
        assert archived is not None
        assert archived.score.components["archive_origin"] == "manual"
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
    assert detail.archive_sub_label == ""
    assert detail.archive_reasons == ()


@pytest.mark.parametrize(
    ("stage", "reasons", "expected"),
    [
        ("local_filter", ["experience_required_too_high:5+ years"], "Filtre automatique"),
        ("rules", [], "Filtre automatique"),
        ("extraction_screening", ["mission_clearly_outside"], "Missions hors cible"),
        ("manual", ["manual_archive"], "Archivage manuel"),
        (None, [], ""),
    ],
)
def test_archived_rows_and_detail_show_only_supported_sub_labels(
    isolated_db, stage, reasons, expected,
) -> None:
    job_id = _job(external_id=f"archive-label:{stage}")
    with session_scope() as session:
        if stage is not None:
            set_score(session, job_id, components={
                "rejection_stage": stage,
                "rejection_reasons": reasons,
            })
        mark_archived(session, job_id)

    service = DesktopService()
    row = next(row for row in service.list_jobs(status=JobStatus.ARCHIVED) if row.id == job_id)
    detail = service.get_job(job_id)
    assert row.status_label == detail.status_label == "Archivée"
    assert row.archive_sub_label == detail.archive_sub_label == expected


def test_confirmed_duplicate_sub_label_takes_priority_over_other_archive_metadata(isolated_db) -> None:
    original_id = _job(external_id="archive-label:original")
    duplicate_id = _job(external_id="archive-label:confirmed-duplicate")
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        duplicate.canonical_job_id = original_id
        duplicate.duplicate_review_status = "confirmed"
        set_score(session, duplicate_id, components={
            "rejection_stage": "manual",
            "rejection_reasons": ["manual_archive"],
        })
        mark_archived(session, duplicate_id)

    service = DesktopService()
    row = next(row for row in service.list_jobs(status=JobStatus.ARCHIVED) if row.id == duplicate_id)
    detail = service.get_job(duplicate_id)
    assert row.archive_sub_label == detail.archive_sub_label == "Doublon confirmé"


def test_explicit_archive_origin_survives_stale_filter_metadata_and_is_cleared_on_rescue(
    isolated_db,
) -> None:
    from smartapply.database.repository import rescue_archived_job

    job_id = _job(external_id="archive-label:rearchived")
    with session_scope() as session:
        set_score(session, job_id, components={"rejection_stage": "local_filter"})
        mark_archived(session, job_id, origin="manual")
    assert DesktopService().get_job(job_id).archive_sub_label == "Archivage manuel"

    with session_scope() as session:
        rescue_archived_job(session, job_id)
        assert "archive_origin" not in session.get(Job, job_id).score.components
        mark_archived(session, job_id, origin="automatic_filter")
    assert DesktopService().get_job(job_id).archive_sub_label == "Filtre automatique"


def test_mark_job_sent_updates_linked_application_and_offer(isolated_db) -> None:
    job_id = _job(external_id="manual:user-sent")
    with session_scope() as session:
        application = create_or_get_application(session, job_id)
        application.cv_pdf_path = "/published/cv.pdf"
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
        assert rescued.ranked_at is None
        assert rescued.score.components["manual_rescue"]["schema_version"] == 3
        assert "filter_disposition" not in rescued.score.components
        assert rescued.score.components["completed_filter_routing"]["disposition"] == "pending_relevance"
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


def test_rescue_duplicate_reopens_review_and_preserves_stale_dossier(isolated_db, monkeypatch) -> None:
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
    monkeypatch.setattr("smartapply.desktop.services._pipeline", lambda *args: pytest.fail("No LLM or generation before duplicate review"))
    report = DesktopService().rescue_job(alias_id)

    assert report["generated"] == 0
    assert report["failed"] == 0
    assert report["job_ids"] == [root_id]
    assert report["duplicate_review_ids"] == [alias_id]
    assert report["application_ids"] == []
    service = DesktopService()
    assert service.get_job(alias_id).status == JobStatus.DUPLICATE_REVIEW
    assert [row.id for row in service.list_jobs(status=JobStatus.DUPLICATE_REVIEW,
                                               only_pending_duplicates=True)] == [alias_id]
    with session_scope() as session:
        assert session.get(Job, alias_id).canonical_job_id is None
        assert session.get(Job, alias_id).possible_duplicate_of_id == root_id
        assert session.get(Job, alias_id).archived_at is None
        assert session.get(Job, root_id).analyzed_at is None
        assert session.get(Application, application_id).cv_json is None
        assert session.query(Application).count() == 1


def test_rescue_alias_with_archived_dossier_is_reviewable(isolated_db, restoration_llm, monkeypatch) -> None:
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
        previous_cv = owner.application.cv_json
        previous_pdf = owner.application.cv_pdf_path

    monkeypatch.setattr("smartapply.desktop.services._pipeline", lambda *args: pytest.fail("No LLM or generation before duplicate review"))
    report = service.rescue_job(owner_id)

    assert report["generated"] == 0
    assert report["failed"] == 0
    assert report["duplicate_review_ids"] == [owner_id]
    assert service.get_job(owner_id).status == JobStatus.DUPLICATE_REVIEW
    assert service.get_job(owner_id).filter_disposition != "rejected"
    assert [row.id for row in service.list_jobs(status=JobStatus.DUPLICATE_REVIEW,
                                               only_pending_duplicates=True)] == [owner_id]
    assert owner_id not in {row.id for row in service.list_jobs(status=JobStatus.ARCHIVED)}
    with session_scope() as session:
        assert session.get(Job, owner_id).canonical_job_id is None
        assert session.get(Job, owner_id).archived_at is None
        assert session.get(Job, owner_id).possible_duplicate_of_id == root_id
        assert session.get(Job, root_id).canonical_job_id is None
        app = session.get(Application, original["application_id"])
        assert app.status == JobStatus.ARCHIVED
        assert app.cv_json == previous_cv and app.cv_pdf_path == previous_pdf
        assert session.query(Application).count() == 1


def test_rescue_keeps_existing_active_dossier_and_deduplicates_selection(
    isolated_db, restoration_llm, monkeypatch
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

    monkeypatch.setattr("smartapply.desktop.services._pipeline", lambda *args: pytest.fail("No LLM or generation before duplicate review"))
    report = service.rescue_jobs(aliases)

    assert report["rescued"] == report["skipped"] == 1
    assert report["existing"] == 0
    assert report["generated"] == report["failed"] == 0
    assert report["duplicate_review_ids"] == [aliases[0]]
    with session_scope() as session:
        assert session.get(Job, root_id).application.id == original["application_id"]
        assert session.get(Job, root_id).application.status == JobStatus.READY_FOR_FORM_SUBMISSION
        assert session.get(Job, aliases[1]).canonical_job_id == root_id


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
        archived_application = create_or_get_application(session, first_id)
        archived_application.cv_pdf_path = "/documents/old-cv.pdf"
        archived_application.status = JobStatus.ARCHIVED
    from smartapply.pipeline import Pipeline

    analyze = Pipeline.analyze_jobs

    def fail_first(self, ids, **kwargs):
        if ids == [first_id]:
            return SimpleNamespace(
                analyzed=0, already_analyzed=0, errors=[{"message": "API indisponible"}]
            )
        return analyze(self, ids, **kwargs)

    monkeypatch.setattr(Pipeline, "analyze_jobs", fail_first)
    report = DesktopService().rescue_jobs([first_id, second_id])

    assert report["generated"] == report["failed"] == 1
    assert "API indisponible" in report["errors"][0]["message"]
    with session_scope() as session:
        failed = session.get(Job, first_id)
        assert failed.archived_at is None
        assert failed.application.status == JobStatus.ARCHIVED
        assert failed.application.cv_pdf_path == "/documents/old-cv.pdf"
        assert failed.analyzed_at is None


def test_bridge_runs_single_rescue_in_background_and_keeps_navigation() -> None:
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
    assert opened == []
    assert toasts[0][2] == "warning"
    assert "1 dossier avec des points à vérifier" in toasts[0][1]
    assert "Détails dans le volet du dossier" in toasts[0][1]


def test_manual_top_selection_persists_across_service_instances(isolated_db) -> None:
    job_id = _job(external_id="manual:persistent-shortlist")
    with session_scope() as session:
        _prepare_analyzed_offer(session, job_id)

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


def test_shortlist_refuses_analyzed_offer_without_ai_fit(isolated_db) -> None:
    job_id = _job(external_id="manual:analyzed-without-ai-fit")
    with session_scope() as session:
        mark_analyzed(session, job_id)
        assert set_shortlisted(session, job_id, selected=True) is None

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.shortlisted_at is None
        assert job.analysis is None


def test_update_shortlist_excludes_current_rejection_and_preserves_manual_choice(
    isolated_db,
) -> None:
    survivor_id = _job(external_id="manual:shortlist-current-survivor")
    rejected_id = _job(external_id="manual:shortlist-current-rejected")
    with session_scope() as session:
        _prepare_analyzed_offer(session, survivor_id)
        _prepare_analyzed_offer(session, rejected_id)
        survivor = session.get(Job, survivor_id)
        rejected = session.get(Job, rejected_id)
        assert survivor is not None and survivor.score is not None
        assert rejected is not None and rejected.score is not None
        survivor.analysis.fit_score = 0.20
        rejected.analysis.fit_score = 0.99
        rejected.score.components = {
            **rejected.score.components,
            "filter_disposition": "rejected",
            "hard_reject": {
                "ruleset_version": "hard-reject-v2",
                "rejected": True,
                "gates": [],
            },
        }
        assert set_shortlisted(
            session,
            rejected_id,
            selected=True,
            origin=ShortlistOrigin.MANUAL,
        ) is not None

    report = DesktopService().update_shortlist(top_k=1)

    assert report["ranked_ids"] == [survivor_id]
    assert report["shortlisted_ids"] == [survivor_id]
    with session_scope() as session:
        rejected = session.get(Job, rejected_id)
        survivor = session.get(Job, survivor_id)
        assert rejected is not None and survivor is not None
        assert rejected.shortlist_origin == ShortlistOrigin.MANUAL
        assert rejected.shortlisted_at is not None
        assert survivor.shortlist_origin == ShortlistOrigin.AUTOMATIC


def test_manual_top_selection_analyzes_unanalyzed_offer_first(isolated_db, monkeypatch) -> None:
    job_id = _job(external_id="manual:auto-analysis-before-shortlist")
    analyzed_calls: list[list[int]] = []

    class FakePipeline:
        def prepare_analysis(self, **kwargs):
            return SimpleNamespace(kept_ids=list(kwargs["job_ids"]))

        def analyze_jobs(self, job_ids, **kwargs):
            analyzed_calls.append(list(job_ids))
            with session_scope() as session:
                mark_filtered(session, job_id)
                set_score(session, job_id, components={"filter_disposition": "pending_relevance"})
                set_analysis(session, job_id, fit_score=0.75)
                mark_analyzed(session, job_id)
            return SimpleNamespace(analyzed=1, already_analyzed=0, errors=[])

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    service = DesktopService()
    assert service.set_job_shortlisted(job_id, selected=True) is False
    assert analyzed_calls == []
    assert service.shortlist_requires_analysis(job_id) is True
    assert service.analyze_and_shortlist([job_id])["shortlisted"] == 1
    detail = DesktopService().get_job(job_id)
    assert detail is not None
    assert analyzed_calls == [[job_id]]
    assert detail.analyzed is True
    assert detail.status == JobStatus.SHORTLISTED


def test_removing_analyzed_offer_from_top_selection_keeps_analyzed_status(isolated_db) -> None:
    job_id = _job(external_id="manual:analyzed-shortlist-removal")
    service = DesktopService()
    with session_scope() as session:
        _prepare_analyzed_offer(session, job_id)

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
        _prepare_analyzed_offer(session, shortlisted_id)
        set_shortlisted(session, shortlisted_id, selected=True)

    new_rows = service.list_jobs(status=JobStatus.SCRAPED)

    assert [row.id for row in new_rows] == [pending_id]
    assert all(row.status == JobStatus.SCRAPED for row in new_rows)


def test_new_offer_list_has_no_implicit_500_row_limit(isolated_db) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for index in range(501):
            job = upsert_job(
                session,
                external_id=f"manual:unbounded-new-{index}",
                title=f"Machine Learning Engineer {index:03d}",
                company="Acme",
                location="Paris",
                contract_type="CDI",
                description="Description",
                source="manual",
            )
            job.filtered_at = now

    service = DesktopService()
    assert len(service.list_jobs(status=JobStatus.SCRAPED)) == 501
    assert len(service.list_jobs(status=JobStatus.SCRAPED, limit=37)) == 37


def test_offer_pages_sort_and_filter_before_pagination(isolated_db) -> None:
    ids = [_job(external_id=f"manual:paged-{index}") for index in range(5)]
    scores = [0.15, 0.80, 0.35, 0.95, 0.60]
    with session_scope() as session:
        for job_id, score in zip(ids, scores, strict=True):
            _prepare_analyzed_offer(session, job_id)
            set_analysis(session, job_id, fit_score=score)
        session.get(Job, ids[1]).source_company = "Target"
        session.get(Job, ids[3]).source_company = "Target"

    service = DesktopService()
    first = service.list_jobs_page(
        status=JobStatus.ANALYZED, sort_key="llm_score", page_size=2
    )
    second = service.list_jobs_page(
        status=JobStatus.ANALYZED, sort_key="llm_score", page=1, page_size=2
    )
    filtered = service.list_jobs_page(
        status=JobStatus.ANALYZED, sort_key="llm_score", search="Target", page_size=1
    )
    assert first["total"] == second["total"] == 5
    assert [row.id for row in first["rows"]] == [ids[3], ids[1]]
    assert [row.id for row in second["rows"]] == [ids[4], ids[2]]
    assert filtered["total"] == 2
    assert [row.id for row in filtered["rows"]] == [ids[3]]

    later_id = _job(external_id="manual:paged-later")
    with session_scope() as session:
        _prepare_analyzed_offer(session, later_id)
        set_analysis(session, later_id, fit_score=0.99)
    refreshed = service.list_jobs_page(
        status=JobStatus.ANALYZED, sort_key="llm_score", page_size=2
    )
    assert refreshed["total"] == 6
    assert [row.id for row in refreshed["rows"]] == [later_id, ids[3]]
    assert [row.id for row in service.list_jobs(status=JobStatus.ANALYZED)[:2]] == [
        later_id, ids[3],
    ]
    assert service.job_page_for(
        ids[2], status=JobStatus.ANALYZED, sort_key="llm_score", page_size=2
    ) == 2


def test_analyzed_minimum_ai_score_filters_before_sort_and_pagination(isolated_db) -> None:
    ids = [_job(external_id=f"minimum-ai-{index}") for index in range(5)]
    with session_scope() as session:
        for job_id, score in zip(ids, [None, 0.0, 0.69, 0.70, 0.90], strict=True):
            _prepare_analyzed_offer(session, job_id)
            set_analysis(session, job_id, fit_score=score)

    service = DesktopService()
    assert len(service.list_jobs(status=JobStatus.ANALYZED, min_llm_score=None)) == 5
    assert [row.id for row in service.list_jobs(
        status=JobStatus.ANALYZED, min_llm_score=0.0,
    )] == [ids[4], ids[3], ids[2], ids[1]]
    first = service.list_jobs_page(
        status=JobStatus.ANALYZED, min_llm_score=0.7,
        sort_key="llm_score", page_size=1,
    )
    second = service.list_jobs_page(
        status=JobStatus.ANALYZED, min_llm_score=0.7,
        sort_key="llm_score", page=1, page_size=1,
    )
    assert first["total"] == second["total"] == 2
    assert [first["rows"][0].id, second["rows"][0].id] == [ids[4], ids[3]]
    assert service.job_page_for(
        ids[3], status=JobStatus.ANALYZED, min_llm_score=0.7,
        sort_key="llm_score", page_size=1,
    ) == 1
    assert service.job_page_for(
        ids[2], status=JobStatus.ANALYZED, min_llm_score=0.7,
        sort_key="llm_score", page_size=1,
    ) is None
    with pytest.raises(ValueError):
        service.list_jobs(status=JobStatus.SCRAPED, min_llm_score=0.7)


def test_minimum_ai_score_preference_persists_and_none_clears_it(isolated_db) -> None:
    service = DesktopService()
    assert service.minimum_llm_score_percent() == -1
    assert service.set_minimum_llm_score_percent(0) == 0
    assert DesktopService().minimum_llm_score_percent() == 0
    assert service.set_minimum_llm_score_percent(73) == 73
    assert DesktopService().minimum_llm_score_percent() == 73
    assert service.set_minimum_llm_score_percent(-1) == -1
    assert DesktopService().minimum_llm_score_percent() == -1


def test_ready_offer_pages_follow_persisted_ai_score(isolated_db) -> None:
    ids = [_job(external_id=f"manual:ready-paged-{index}") for index in range(3)]
    with session_scope() as session:
        for job_id, score in zip(ids, [0.50, 0.90, 0.70], strict=True):
            _prepare_analyzed_offer(session, job_id)
            set_analysis(session, job_id, fit_score=score)
            application = create_or_get_application(session, job_id)
            application.status = JobStatus.READY_FOR_FORM_SUBMISSION

    page = DesktopService().list_jobs_page(
        status=JobStatus.READY_FOR_FORM_SUBMISSION,
        sort_key="llm_score", page_size=2,
    )
    assert page["total"] == 3
    assert [row.id for row in page["rows"]] == [ids[1], ids[2]]
    assert [row.id for row in DesktopService().list_jobs(
        status=JobStatus.READY_FOR_FORM_SUBMISSION
    )] == [ids[1], ids[2], ids[0]]


@pytest.mark.parametrize("status", [JobStatus.READY_FOR_FORM_SUBMISSION, JobStatus.SENT])
def test_minimum_ai_score_filters_ready_and_sent_offers(isolated_db, status) -> None:
    ids = [_job(external_id=f"manual:{status}-minimum-{index}") for index in range(3)]
    with session_scope() as session:
        for job_id, score in zip(ids, [0.50, 0.70, 0.90], strict=True):
            _prepare_analyzed_offer(session, job_id)
            set_analysis(session, job_id, fit_score=score)
            create_or_get_application(session, job_id).status = status

    service = DesktopService()
    first = service.list_jobs_page(status=status, min_llm_score=0.7, page_size=1)
    second = service.list_jobs_page(status=status, min_llm_score=0.7, page=1, page_size=1)
    assert first["total"] == second["total"] == 2
    assert [first["rows"][0].id, second["rows"][0].id] == [ids[2], ids[1]]
    assert service.job_page_for(ids[0], status=status, min_llm_score=0.7, page_size=1) is None
    assert [row.id for row in service.list_jobs(status=status, min_llm_score=0.7)] == [ids[2], ids[1]]


def test_new_offer_disposition_filter_is_exact_and_sql_scoped(isolated_db) -> None:
    legacy_relevant_id = _job(external_id="manual:class-legacy-relevant")
    rejected_id = _job(external_id="manual:class-rejected")
    pending_id = _job(external_id="manual:class-pending")
    uncertain_id = _job(external_id="manual:class-legacy-uncertain")
    missing_score_id = _job(external_id="manual:class-no-score")
    analyzed_pending_id = _job(external_id="manual:class-analyzed-pending")
    dispositions = {
        legacy_relevant_id: "relevant",
        rejected_id: "rejected",
        pending_id: "pending_relevance",
        uncertain_id: "uncertain",
        analyzed_pending_id: "pending_relevance",
    }
    with session_scope() as session:
        for job_id in (*dispositions, missing_score_id):
            mark_filtered(session, job_id)
        for job_id, disposition in dispositions.items():
            set_score(
                session,
                job_id,
                components={"filter_disposition": disposition},
            )
        mark_analyzed(session, analyzed_pending_id)

    service = DesktopService()
    unfiltered_ids = {row.id for row in service.list_jobs(status=JobStatus.SCRAPED)}
    assert unfiltered_ids == {
        legacy_relevant_id,
        rejected_id,
        pending_id,
        uncertain_id,
        missing_score_id,
    }
    assert {
        disposition: [row.id for row in service.list_jobs(
            status=JobStatus.SCRAPED,
            filter_disposition=disposition,
        )]
        for disposition in ("rejected", "pending_relevance")
    } == {
        "rejected": [rejected_id],
        "pending_relevance": [pending_id],
    }
    assert [row.id for row in service.list_jobs(
        status=JobStatus.SCRAPED,
        sort_key="filter_disposition",
        sort_ascending=True,
    )] == [
        pending_id,
        rejected_id,
        legacy_relevant_id,
        uncertain_id,
        missing_score_id,
    ]


@pytest.mark.parametrize("disposition", ["relevant", "uncertain", "pending"])
def test_disposition_filter_rejects_legacy_and_unknown_classes(
    isolated_db,
    disposition,
) -> None:
    service = DesktopService()
    with pytest.raises(ValueError, match="Unsupported filter disposition"):
        service.list_jobs(status=JobStatus.SCRAPED, filter_disposition=disposition)


def test_disposition_filter_rejects_non_new_statuses(isolated_db) -> None:
    service = DesktopService()
    with pytest.raises(ValueError, match="only for new offers"):
        service.list_jobs(status=JobStatus.ANALYZED, filter_disposition="pending_relevance")


def test_duplicate_active_filter_accepts_legacy_non_rejected_offers(isolated_db) -> None:
    both_parent = _job(external_id="manual:duplicate-both-parent")
    both_child = _job(external_id="manual:duplicate-both-child")
    legacy_parent = _job(external_id="manual:duplicate-legacy-parent")
    legacy_child = _job(external_id="manual:duplicate-legacy-child")
    legacy_child_parent = _job(external_id="manual:duplicate-legacy-child-parent")
    legacy_child_child = _job(external_id="manual:duplicate-legacy-child-child")
    dispositions = {
        both_parent: "pending_relevance",
        both_child: "pending_relevance",
        legacy_parent: "relevant",
        legacy_child: "pending_relevance",
        legacy_child_parent: "pending_relevance",
        legacy_child_child: "relevant",
    }
    with session_scope() as session:
        for job_id, disposition in dispositions.items():
            set_score(
                session,
                job_id,
                components={"filter_disposition": disposition},
            )
        for child_id, parent_id in (
            (both_child, both_parent),
            (legacy_child, legacy_parent),
            (legacy_child_child, legacy_child_parent),
        ):
            child = session.get(Job, child_id)
            child.possible_duplicate_of_id = parent_id
            child.duplicate_review_status = "pending"

    service = DesktopService()
    assert {
        row.id for row in service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    } == {both_child, legacy_child, legacy_child_child}
    assert {row.id for row in service.list_jobs(
        status=JobStatus.DUPLICATE_REVIEW,
        only_pending_duplicates=True,
    )} == {both_child, legacy_child, legacy_child_child}
    page = service.list_jobs_page(
        status=JobStatus.DUPLICATE_REVIEW, only_pending_duplicates=True,
        page=1, page_size=1,
    )
    assert page["total"] == 3 and len(page["rows"]) == 1


@pytest.mark.parametrize(
    "child_disposition,parent_disposition,child_state,parent_state,visible",
    [
        ("pending_relevance", "rejected", "filtered", "filtered", True),
        ("rejected", "pending_relevance", "filtered", "filtered", True),
        ("rejected", "rejected", "filtered", "filtered", False),
        ("pending_relevance", "pending_relevance", "archived", "filtered", True),
        ("pending_relevance", "pending_relevance", "filtered", "archived", True),
        ("pending_relevance", "pending_relevance", "archived", "archived", False),
        ("rejected", "pending_relevance", "filtered", "archived", False),
        (None, None, "analyzed", "ready_for_form_submission", True),
        ("rejected_history", None, "analyzed", "archived", False),
        ("pending_relevance", "rejected", "application_archived", "filtered", False),
        (None, "uncertain", "filtered", "filtered", True),
    ],
)
def test_duplicate_active_filter_requires_one_non_rejected_non_archived_offer(
    isolated_db, child_disposition, parent_disposition, child_state, parent_state, visible,
) -> None:
    parent_id = _job(external_id="manual:active-pair-parent")
    child_id = _job(external_id="manual:active-pair-child")
    with session_scope() as session:
        for job_id, disposition, state in (
            (child_id, child_disposition, child_state),
            (parent_id, parent_disposition, parent_state),
        ):
            components = (
                {"completed_filter_routing": {"disposition": "rejected"}}
                if disposition == "rejected_history" else
                {"filter_disposition": disposition} if disposition else {}
            )
            set_score(session, job_id, components=components)
            job = session.get(Job, job_id)
            if state.startswith("application_"):
                create_or_get_application(session, job_id).status = state.removeprefix("application_")
            else:
                job.status = state
            if state == "archived":
                job.archived_at = datetime.now(timezone.utc)
        child = session.get(Job, child_id)
        child.possible_duplicate_of_id = parent_id
        child.duplicate_review_status = "pending"

    def snapshot():
        with session_scope() as session:
            return [
                (job.id, job.status, job.archived_at, job.possible_duplicate_of_id,
                 job.duplicate_review_status, job.score.components,
                 job.application.status if job.application else None)
                for job in (session.get(Job, parent_id), session.get(Job, child_id))
            ]

    before = snapshot()
    rows = DesktopService().list_jobs(
        status=JobStatus.DUPLICATE_REVIEW, only_pending_duplicates=True,
    )
    assert [row.id for row in rows] == ([child_id] if visible else [])
    assert snapshot() == before


def test_shortlist_filter_excludes_generated_applications(isolated_db) -> None:
    shortlisted_id = _job(external_id="manual:shortlist-without-app")
    generated_id = _job(external_id="manual:shortlist-with-app")
    with session_scope() as session:
        for job_id in (shortlisted_id, generated_id):
            _prepare_analyzed_offer(session, job_id)
            set_shortlisted(session, job_id, selected=True)
        create_or_get_application(session, generated_id)

    rows = DesktopService().list_jobs(status=JobStatus.SHORTLISTED)

    assert [row.id for row in rows] == [shortlisted_id]


def test_shortlist_summary_ignores_unanalyzed_legacy_rows(isolated_db) -> None:
    valid_id = _job(external_id="manual:valid-summary-shortlist")
    stale_id = _job(external_id="manual:stale-summary-shortlist")
    with session_scope() as session:
        _prepare_analyzed_offer(session, valid_id)
        assert set_shortlisted(session, valid_id, selected=True) is not None
        stale = session.get(Job, stale_id)
        assert stale is not None
        stale.shortlisted_at = datetime.now(timezone.utc)
        stale.status = JobStatus.SHORTLISTED

    service = DesktopService()

    assert service.shortlist_summary().total == 1
    assert service.shortlisted_generation_ids() == [valid_id]


def test_job_statuses_include_analysis_filter_but_hide_internal_states(isolated_db) -> None:
    statuses = DesktopService.job_statuses()

    assert JobStatus.FILTERED not in {value for value, _label in statuses}
    assert JobStatus.DUPLICATE_REVIEW not in {value for value, _label in statuses}
    assert [value for value, _label in statuses] == [
        JobStatus.SCRAPED,
        JobStatus.ANALYZED,
        JobStatus.SHORTLISTED,
        JobStatus.READY_FOR_FORM_SUBMISSION,
        JobStatus.SENT,
        JobStatus.ARCHIVED,
    ]
    assert statuses[0][1] == "À analyser"


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
        _prepare_analyzed_offer(session, first_id)
        _prepare_analyzed_offer(session, second_id)
        set_shortlisted(session, first_id, selected=True)
        set_shortlisted(session, second_id, selected=True)

    analyzed_calls: list[int] = []
    generated_calls: list[int] = []

    class FakePipeline:
        def prepare_analysis(self, **kwargs):
            return SimpleNamespace(kept_ids=list(kwargs["job_ids"]))

        def analyze_jobs(self, job_ids, **kwargs):
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
        def prepare_analysis(self, **kwargs):
            return SimpleNamespace(kept_ids=list(kwargs["job_ids"]))

        def analyze_jobs(self, job_ids, **kwargs):
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
        def prepare_analysis(self, **kwargs):
            return SimpleNamespace(kept_ids=list(kwargs["job_ids"]))

        def analyze_jobs(self, job_ids, **kwargs):
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
            "application_id": 73,
            "title": "Machine Learning Engineer",
            "company": "Acme",
            "message": "cv_pdf_not_generated",
            "issues": ["cv_pdf_not_generated"],
        }
    ]


def test_manual_generation_returns_document_warnings(isolated_db, monkeypatch) -> None:
    from smartapply.pipeline import ApplyReport

    class FakePipeline:
        def prepare_analysis(self, **kwargs):
            return SimpleNamespace(kept_ids=list(kwargs["job_ids"]))

        def analyze_jobs(self, job_ids, **kwargs):
            assert len(job_ids) == 1
            return SimpleNamespace(analyzed=1, already_analyzed=0)

        def apply_to(self, job_id, *, form_url):
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


def test_regenerate_application_reuses_ready_dossier_and_persisted_folder(
    isolated_db, tmp_path, monkeypatch
) -> None:
    from smartapply.pipeline import ApplyReport

    job_id = _job(external_id="manual:regenerate-ready")
    legacy_folder = tmp_path / "previous-output" / "application-42"
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        application = create_or_get_application(session, job_id)
        application.status = JobStatus.READY_FOR_FORM_SUBMISSION
        application.cv_pdf_path = str(legacy_folder / "CV.pdf")
        application.form_submission_url = "https://example.test/form"
        job.status = JobStatus.READY_FOR_FORM_SUBMISSION
        application_id = application.id

    calls = []

    class FakePipeline:
        def apply_to(
            self,
            received_job_id,
            *,
            form_url,
            force_regenerate,
            artifact_dir,
        ):
            calls.append(
                {
                    "job_id": received_job_id,
                    "form_url": form_url,
                    "force_regenerate": force_regenerate,
                    "artifact_dir": artifact_dir,
                }
            )
            return ApplyReport(job_id=received_job_id, application_id=application_id)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)

    report = DesktopService().regenerate_application(job_id)

    assert report["regenerated"] is True
    assert calls == [
        {
            "job_id": job_id,
            "form_url": "https://example.test/form",
            "force_regenerate": True,
            "artifact_dir": legacy_folder.resolve(),
        }
    ]


def test_bridge_reports_existing_application_without_changing_navigation() -> None:
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._refresh_workflow = lambda: None
    bridge._set_activity = lambda *args: None
    opened = []
    notifications = []
    bridge.openApplication = opened.append
    bridge.toastRequested.connect(lambda *args: notifications.append(args))

    bridge._generation_done({"job_id": 705, "application_id": 127, "existing": True})

    assert opened == []
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


def test_search_progress_keeps_linkedin_visible_while_wttj_finishes(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.pipeline.ingest import IngestCollection, IngestReport

    linkedin_visible = Event()
    messages = []

    def progress(message):
        messages.append(message)
        if message == "Recherche en cours : LinkedIn…":
            linkedin_visible.set()

    class FakePipeline:
        def collect_source(self, source, *args, **kwargs):  # noqa: ARG002
            if source == "linkedin":
                assert linkedin_visible.wait(timeout=2)
            return IngestCollection(source=source, raw_jobs=[])

        def persist_collection(self, collection):
            return IngestReport(source=collection.source, fetched=0, persisted=0)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", FakePipeline)
    result = DesktopService().search_jobs(
        query="Data Scientist",
        location="Paris",
        sources=["linkedin", "welcometothejungle"],
        max_results=100,
        date_posted="week",
        progress=progress,
    )

    assert messages[:2] == [
        "Recherche en cours : LinkedIn, Welcome to the Jungle…",
        "Recherche en cours : LinkedIn…",
    ]
    assert not result.errors


def test_search_propagates_cooperative_cancellation(
    isolated_db,
    monkeypatch,
) -> None:
    from smartapply.pipeline.ingest import IngestCollection, IngestReport

    class FakePipeline:
        def __init__(self, *, stop_requested=None):
            self.stop_requested = stop_requested

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
