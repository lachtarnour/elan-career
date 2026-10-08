"""Strict copy cards preserve all offers and individual review decisions."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, select

from smartapply.database import Application, Job, JobStatus, session_scope
from smartapply.database.models import JobDuplicateStatus
from smartapply.database.repository import create_or_get_application, set_score, upsert_job
from smartapply.desktop.services import DesktopService

DESCRIPTION = (
    "Vous développez et déployez des modèles de machine learning en Python. "
    "Vous évaluez les modèles, préparez les données et travaillez avec les équipes produit. "
    "Vous maintenez les pipelines de production et documentez les résultats. "
) * 3


def _offer(session, external_id, *, source="manual", description=DESCRIPTION, age=0):
    offer = upsert_job(
        session,
        external_id=external_id,
        title="Machine Learning Engineer",
        company="Acme",
        location="Paris",
        contract_type="CDI",
        description=description,
        source=source,
        application_url=f"https://example.test/offers/{external_id}",
    )
    offer.scraped_at = datetime(2026, 10, 6, tzinfo=timezone.utc) - timedelta(days=age)
    session.flush()
    return offer


def _pending(offer, reference):
    offer.possible_duplicate_of_id = reference.id
    offer.duplicate_review_status = JobDuplicateStatus.PENDING
    offer.duplicate_confidence = 0.99


def _snapshot(session):
    return {
        "jobs": [
            tuple(getattr(job, column.name) for column in Job.__table__.columns)
            for job in session.execute(select(Job).order_by(Job.id)).scalars()
        ],
        "applications": [
            tuple(getattr(app, column.name) for column in Application.__table__.columns)
            for app in session.execute(select(Application).order_by(Application.id)).scalars()
        ],
    }


def test_copy_card_keeps_every_pair_and_source_without_persisting_changes(isolated_db):
    with session_scope() as session:
        reference = _offer(session, "reference", source="linkedin", age=2)
        older = _offer(session, "older", source="serpapi", age=1)
        recent = _offer(session, "recent", source="francetravail")
        different = _offer(session, "different", description=DESCRIPTION + "Responsabilité supplémentaire.")
        for offer in (older, recent, different):
            _pending(offer, reference)
        application = create_or_get_application(session, reference.id)
        application.status = JobStatus.SENT
        application.form_submitted_at = datetime(2026, 10, 4, tzinfo=timezone.utc)
        application.cv_pdf_path = "/published/original-cv.pdf"
        set_score(session, reference.id, final_score=None, components={"filter_disposition": "rejected"})
        reference_id, older_id, recent_id, different_id = (
            reference.id, older.id, recent.id, different.id
        )
    with session_scope() as session:
        before = _snapshot(session)

    rows = DesktopService().list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    by_id = {row.id: row for row in rows}
    assert set(by_id) == {older_id, recent_id, different_id}
    expected_ids = (recent_id, older_id, reference_id)
    assert by_id[older_id].strict_copy_group_ids == expected_ids
    assert by_id[recent_id].strict_copy_group_ids == expected_ids
    assert not by_id[different_id].strict_copy_group_ids
    versions = by_id[recent_id].strict_copy_versions
    assert {version.source for version in versions} == {"linkedin", "serpapi", "francetravail"}
    reference_version = next(version for version in versions if version.id == reference_id)
    assert reference_version.status_label == "Envoyée"
    assert reference_version.filter_disposition == ""
    with session_scope() as session:
        assert _snapshot(session) == before


def test_single_pending_copy_exposes_its_reference_and_confirmed_alias(isolated_db):
    with session_scope() as session:
        reference = _offer(session, "reference", age=1)
        alias = _offer(session, "confirmed-alias", age=2)
        alias.canonical_job_id = reference.id
        alias.duplicate_review_status = JobDuplicateStatus.CONFIRMED
        recent = _offer(session, "recent")
        _pending(recent, reference)
        ids = {reference.id, alias.id, recent.id}

    rows = DesktopService().list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    assert len(rows) == 1
    assert set(rows[0].strict_copy_group_ids) == ids
    assert {version.id for version in rows[0].strict_copy_versions} == ids


def test_active_offer_switch_keeps_pair_with_one_rejected_copy(isolated_db):
    with session_scope() as session:
        reference = _offer(session, "reference")
        recent = _offer(session, "recent")
        _pending(recent, reference)
        for offer, disposition in ((reference, "rejected"), (recent, "pending_relevance")):
            set_score(session, offer.id, final_score=None, components={"filter_disposition": disposition})

    service = DesktopService()
    assert len(service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)) == 1
    assert len(service.list_jobs(
        status=JobStatus.DUPLICATE_REVIEW, only_pending_duplicates=True,
    )) == 1


def test_normal_list_does_not_load_copy_content_or_build_copy_groups(isolated_db, monkeypatch):
    with session_scope() as session:
        _offer(session, "normal")
        engine = session.get_bind()
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    def unexpected_grouping(_offers):
        raise AssertionError("Normal lists must not build copy groups")

    monkeypatch.setattr("smartapply.desktop.services.group_strict_copies", unexpected_grouping)
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        rows = DesktopService().list_jobs()
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert len(rows) == 1
    assert not rows[0].strict_copy_group_ids
    assert not any("jobs.description" in statement for statement in statements)
