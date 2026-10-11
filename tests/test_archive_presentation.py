"""Archive causes must follow dossier history rather than filter diagnostics."""

from datetime import datetime, timedelta, timezone

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Application, Job, JobScore, JobStatus
from smartapply.database.repository import mark_archived, rescue_archived_job
from smartapply.desktop.services import DesktopService


def _job(session, key, *, components=None, archived=True):
    job = Job(
        external_id=key, title="Data Scientist", source_company="MP DATA",
        source="fixture", description="Concevoir des modèles de Machine Learning.",
        status=JobStatus.ARCHIVED if archived else JobStatus.FILTERED,
        archived_at=datetime.now(timezone.utc) if archived else None,
    )
    if components is not None:
        job.score = JobScore(components=components)
    session.add(job)
    session.flush()
    return job


@pytest.mark.parametrize("audit", [None, {"rejected": False, "gates": [
    {"gate": "role", "status": "clear", "notes": ["role_title_veto:candidate_ai_mission"]},
    {"gate": "contract", "status": "unresolved", "conflicts": ["requires_review"]},
]}])
def test_archived_dossier_does_not_turn_filter_pass_into_archive_cause(isolated_db, audit):
    components = {"reasons": ["role_title_veto:candidate_ai_mission"],
                  "filter_disposition": "pending_relevance"}
    if audit is not None:
        components["hard_reject"] = audit
    with session_scope() as session:
        job = _job(session, "positive", components=components, archived=False)
        job.application = Application(status=JobStatus.ARCHIVED)
        job_id = job.id
    detail = DesktopService().get_job(job_id)
    assert detail.archive_reasons == ("Raison d’archivage non enregistrée",)
    with session_scope() as session:
        assert session.get(Job, job_id).score.components == components


def test_missing_rejection_list_uses_only_blocked_gate_codes(isolated_db):
    with session_scope() as session:
        job = _job(session, "blocked", components={
            "reasons": ["role_title_veto:candidate_ai_mission", "seniority_in_title:staff"],
            "hard_reject": {"rejected": True, "gates": [
                {"gate": "seniority", "status": "blocked", "reason_codes": ["seniority_in_title:staff"]},
                {"gate": "role", "status": "clear", "reason_codes": ["off_target_role_family:business"],
                 "notes": ["role_title_veto:candidate_ai_mission"]},
            ]},
        })
        job_id = job.id
    assert DesktopService().get_job(job_id).archive_reasons == (
        "Niveau de séniorité trop élevé : staff",
    )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_legacy_duplicate_merge_recovers_only_unique_documented_source(isolated_db, ambiguous):
    with session_scope() as session:
        winner = _job(session, "winner", components={
            "reasons": ["role_title_veto:candidate_ai_mission"],
            "hard_reject": {"rejected": False, "gates": []},
        })
        application = Application(status=JobStatus.ARCHIVED, form_submission_url="https://example.test/original")
        archived_at = datetime.now(timezone.utc) - timedelta(days=2)
        application.created_at = archived_at - timedelta(days=1)
        application.updated_at = archived_at
        winner.application = application
        source_ids = []
        for index in range(2 if ambiguous else 1):
            alias = _job(session, f"source-{index}", components={
                "rejection_stage": "manual", "rejection_reasons": ["manual_archive"],
            })
            alias.canonical_job_id = winner.id
            alias.archived_at = archived_at + timedelta(milliseconds=5)
            alias.duplicate_review_status = "confirmed"
            alias.application_url = application.form_submission_url
            source_ids.append(alias.id)
        session.flush()
        winner_id, application_id = winner.id, application.id
    detail = DesktopService().get_job(winner_id)
    if ambiguous:
        assert detail.archive_reasons == ("Raison d’archivage non enregistrée",)
    else:
        assert detail.archive_reasons == (
            f"Archivage conservé après regroupement d’un doublon : dossier n° {application_id}, "
            f"annonce d’origine « Data Scientist — MP DATA » (n° {source_ids[0]})",
            "Motif d’origine : Archivée manuellement",
        )
    with session_scope() as session:
        assert "archive_inheritance" not in session.get(Job, winner_id).score.components


def test_legacy_merge_ignores_other_alias_with_same_url_but_different_event(isolated_db):
    with session_scope() as session:
        winner = _job(session, "winner-two-aliases")
        event = datetime.now(timezone.utc) - timedelta(days=2)
        winner.application = Application(
            status=JobStatus.ARCHIVED, form_submission_url="https://example.test/original",
            created_at=event - timedelta(days=1), updated_at=event,
        )
        source = _job(session, "valid-alias", components={
            "rejection_stage": "manual", "rejection_reasons": ["manual_archive"],
        })
        source.canonical_job_id = winner.id
        source.duplicate_review_status = "confirmed"
        source.application_url = winner.application.form_submission_url
        source.archived_at = event + timedelta(milliseconds=5)
        other = _job(session, "other-alias", components={})
        other.canonical_job_id = winner.id
        other.duplicate_review_status = "confirmed"
        other.application_url = winner.application.form_submission_url
        other.archived_at = event + timedelta(days=1)
        winner_id = winner.id
    assert DesktopService().get_job(winner_id).archive_reasons[-1] == "Motif d’origine : Archivée manuellement"


def test_tracking_archive_records_manual_cause(isolated_db):
    from smartapply.database.repository import update_application_tracking

    with session_scope() as session:
        job = _job(session, "tracking-archive", archived=False)
        job.application = Application(status=JobStatus.ANALYZED)
        session.flush()
        job_id, application_id = job.id, job.application.id
        update_application_tracking(session, application_id, status=JobStatus.ARCHIVED)
        assert job.status == JobStatus.ARCHIVED
        assert job.archived_at is not None
    with session_scope() as session:
        assert session.get(Job, job_id).score.components["archive_origin"] == "manual"
    assert DesktopService().get_job(job_id).archive_reasons == ("Archivée manuellement",)


def test_pending_alias_does_not_prove_inherited_archive(isolated_db):
    with session_scope() as session:
        winner = _job(session, "winner-pending")
        winner.application = Application(status=JobStatus.ARCHIVED, form_submission_url="https://example.test/source")
        alias = _job(session, "pending-source", components={"rejection_stage": "manual"})
        alias.canonical_job_id = winner.id
        alias.duplicate_review_status = "pending"
        alias.application_url = winner.application.form_submission_url
        winner_id = winner.id
    assert DesktopService().get_job(winner_id).archive_reasons == ("Raison d’archivage non enregistrée",)
    assert DesktopService().get_job(alias.id).archive_reasons == ("Archivée manuellement",)


def _inheritance(application_id):
    return {
        "schema_version": 1, "source_job_id": 2037, "source_title": "Data Scientist",
        "source_company": "MP DATA", "application_id": application_id,
        "origin": "manual", "rejection_stage": "manual", "rejection_reasons": ["manual_archive"],
    }


def test_inheritance_belongs_to_current_archived_dossier(isolated_db):
    with session_scope() as session:
        job = _job(session, "replaced-dossier")
        job.application = Application(status=JobStatus.ARCHIVED)
        session.flush()
        job.score = JobScore(components={"archive_inheritance": _inheritance(job.application.id + 1)})
        job_id = job.id
    assert DesktopService().get_job(job_id).archive_reasons == ("Raison d’archivage non enregistrée",)


def test_source_archived_later_cannot_explain_dossier_archive(isolated_db):
    with session_scope() as session:
        winner = _job(session, "unrelated-archive")
        event = datetime.now(timezone.utc) - timedelta(days=2)
        winner.application = Application(
            status=JobStatus.ARCHIVED, form_submission_url="https://example.test/old",
            created_at=event - timedelta(days=1), updated_at=event,
        )
        alias = _job(session, "later-source", components={"rejection_stage": "manual"})
        alias.canonical_job_id = winner.id
        alias.duplicate_review_status = "confirmed"
        alias.application_url = winner.application.form_submission_url
        winner_id = winner.id
    assert DesktopService().get_job(winner_id).archive_reasons == ("Raison d’archivage non enregistrée",)


def test_explicit_manual_origin_takes_priority_over_stale_filter_causes(isolated_db):
    with session_scope() as session:
        job = _job(session, "manual-origin", components={
            "archive_origin": "manual", "rejection_stage": "local_filter",
            "rejection_reasons": ["experience_required_too_high:5+ years"],
        })
        job_id = job.id
    detail = DesktopService().get_job(job_id)
    assert detail.archive_sub_label == "Archivage manuel"
    assert detail.archive_reasons == ("Archivée manuellement",)


@pytest.mark.parametrize("restore", [False, True])
def test_new_manual_archive_replaces_inherited_cause(isolated_db, restore):
    with session_scope() as session:
        job = _job(session, "new-archive")
        job.application = Application(status=JobStatus.ARCHIVED)
        session.flush()
        inheritance = _inheritance(job.application.id)
        job.score = JobScore(components={"archive_inheritance": inheritance, "archive_origin": "manual"})
        job_id = job.id
    if restore:
        with session_scope() as session:
            rescue_archived_job(session, job_id)
            assert session.get(Job, job_id).score.components["manual_rescue"]["previous_rejection"]["archive_inheritance"] == inheritance
    else:
        assert "regroupement" in DesktopService().get_job(job_id).archive_reasons[0]
    with session_scope() as session:
        mark_archived(session, job_id, origin="manual")
        assert "archive_inheritance" not in session.get(Job, job_id).score.components
    assert DesktopService().get_job(job_id).archive_reasons == ("Archivée manuellement",)
