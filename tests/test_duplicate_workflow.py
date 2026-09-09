"""Regression tests for offer identity, duplicate review and application guards."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from smartapply.database import (
    Application,
    Job,
    JobDuplicateStatus,
    JobStatus,
    backfill_duplicate_reviews,
    session_scope,
)
from smartapply.database.repository import (
    application_for_duplicate_group,
    application_ids_for_confirmed_groups,
    canonical_job,
    create_or_get_application,
    duplicate_group_ids,
    list_pending_processing,
    upsert_job,
)
from smartapply.desktop.services import DesktopService
from smartapply.jobsearch.workflow import APPLICATION_STATUSES
from smartapply.offers import RawJob
from smartapply.pipeline.apply.persistence import ApplicationPersistenceMixin
from smartapply.pipeline.errors import ApplicationAlreadyExistsError, DuplicateReviewRequiredError
from smartapply.pipeline.ingestor import Ingestor
from smartapply.pipeline.reports import ApplyReport

_DESCRIPTION = (
    "Nous recherchons un Data Scientist pour développer des pipelines de données "
    "et des modèles Python et PyTorch pour notre équipe produit."
)


def _raw(
    external_id: str,
    url: str,
    *,
    title: str = "Data Scientist",
    source: str = "serpapi",
) -> RawJob:
    return RawJob(
        external_id=external_id,
        title=title,
        company="Acme SAS",
        location="Paris",
        description=_DESCRIPTION,
        application_url=url,
        source=source,
    )


def _create_review_pair() -> tuple[int, int]:
    ingestor = Ingestor()
    first = ingestor._persist("serpapi", [_raw("serpapi:source-1", "https://acme.test/jobs/1")])
    second = ingestor._persist("serpapi", [_raw("serpapi:source-2", "https://acme.test/jobs/2")])
    assert first.job_ids and second.job_ids
    assert second.duplicate_review_ids == second.job_ids
    return first.job_ids[0], second.job_ids[0]


def test_same_source_id_updates_one_offer_but_same_label_with_new_id_is_held(
    isolated_db,
) -> None:
    ingestor = Ingestor()
    first = ingestor._persist(
        "francetravail",
        [_raw("francetravail:123", "https://acme.test/jobs/123", title="Data Scientist")],
    )
    refreshed = ingestor._persist(
        "francetravail",
        [_raw("francetravail:123", "https://acme.test/jobs/123", title="Data Scientist corrigé")],
    )
    probable = ingestor._persist(
        "francetravail",
        [_raw("francetravail:456", "https://acme.test/jobs/456", title="Data Scientist")],
    )

    with session_scope() as session:
        jobs = session.query(Job).order_by(Job.id.asc()).all()
        assert len(jobs) == 2
        assert jobs[0].title == "Data Scientist corrigé"
        assert jobs[1].duplicate_review_status == JobDuplicateStatus.PENDING
        assert jobs[1].possible_duplicate_of_id == first.job_ids[0]
    assert refreshed.job_ids == first.job_ids
    assert probable.duplicate_review_ids == probable.job_ids


@pytest.mark.parametrize(
    "retry_url", ["https://acme.test/jobs/2", "https://acme.test/jobs/2?utm_source=retry"]
)
def test_manual_retry_reports_pending_review_until_user_resolves_it(
    isolated_db,
    retry_url: str,
) -> None:
    ingestor = Ingestor()
    root = ingestor._persist("serpapi", [_raw("serpapi:original", "https://acme.test/jobs/1")])
    with session_scope() as session:
        original = session.get(Job, root.job_ids[0])
        original.archived_at = datetime.now(timezone.utc)
        original.status = JobStatus.ARCHIVED

    def submit(url: str):
        return ingestor.from_text(
            _DESCRIPTION,
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            application_url=url,
        )

    first = submit("https://acme.test/jobs/2")
    retry = submit(retry_url)

    assert retry.job_ids == first.job_ids
    assert retry.duplicate_review_ids == first.duplicate_review_ids == first.job_ids
    with session_scope() as session:
        assert session.query(Job).count() == 2
        duplicate = session.get(Job, first.job_ids[0])
        assert duplicate.possible_duplicate_of_id == root.job_ids[0]

    assert DesktopService().resolve_duplicate(first.job_ids[0], same_offer=False)
    resolved = submit(retry_url)
    assert resolved.job_ids == first.job_ids
    assert resolved.duplicate_review_ids == []


def test_manual_application_retry_explains_duplicate_review(isolated_db) -> None:
    root = Ingestor()._persist("serpapi", [_raw("serpapi:original", "https://acme.test/jobs/1")])
    service = DesktopService()
    blocked_ids: list[int] = []

    for _ in range(2):
        with pytest.raises(DuplicateReviewRequiredError) as error:
            service.create_manual_application(
                title="Data Scientist",
                company="Acme SAS",
                location="Paris",
                description=_DESCRIPTION,
                application_url="https://acme.test/jobs/2",
            )
        assert error.value.candidate_job_id == root.job_ids[0]
        blocked_ids.append(error.value.job_id)

    assert blocked_ids[0] == blocked_ids[1]
    with session_scope() as session:
        assert session.query(Application).count() == 0


def test_startup_backfill_protects_legacy_rows_without_touching_application_history(
    isolated_db,
) -> None:
    with session_scope() as session:
        root = upsert_job(
            session,
            external_id="legacy:root",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/legacy/root",
            source="serpapi",
        )
        duplicate = upsert_job(
            session,
            external_id="legacy:duplicate",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/legacy/duplicate",
            source="linkedin",
        )
        application = create_or_get_application(session, root.id)
        application.status = JobStatus.ARCHIVED

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        reviewed = session.get(Job, duplicate.id)
        preserved_application = session.get(Application, application.id)
        assert reviewed is not None
        assert reviewed.duplicate_review_status == JobDuplicateStatus.PENDING
        assert reviewed.possible_duplicate_of_id == root.id
        assert preserved_application is not None
        assert preserved_application.status == JobStatus.ARCHIVED


def test_startup_backfill_closes_legacy_pair_when_both_offers_are_archived(
    isolated_db,
) -> None:
    with session_scope() as session:
        root = upsert_job(
            session,
            external_id="legacy:archived-root",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/archived/root",
            source="serpapi",
        )
        duplicate = upsert_job(
            session,
            external_id="legacy:archived-duplicate",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/archived/duplicate",
            source="linkedin",
        )
        for job in (root, duplicate):
            assert job is not None
            job.archived_at = datetime.now(timezone.utc)
            job.status = JobStatus.ARCHIVED
        duplicate_id = duplicate.id

    # The pair is ignored without creating a pending human review.
    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.REJECTED
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.status == JobStatus.ARCHIVED


def test_same_direct_offer_url_becomes_an_archived_alias(isolated_db) -> None:
    ingestor = Ingestor()
    first = ingestor._persist(
        "serpapi",
        [_raw("serpapi:old-id", "https://acme.test/jobs/canonical", source="serpapi")],
    )
    second = ingestor._persist(
        "linkedin",
        [_raw("linkedin:new-id", "https://acme.test/jobs/canonical", source="linkedin")],
    )

    assert second.aliases_created == 1
    with session_scope() as session:
        alias = session.query(Job).filter(Job.external_id == "linkedin:new-id").one()
        assert alias is not None
        assert alias.canonical_job_id == first.job_ids[0]
        assert alias.duplicate_review_status == JobDuplicateStatus.CONFIRMED
        assert alias.status == JobStatus.ARCHIVED
        assert alias.shortlisted_at is None


def test_probable_duplicate_is_visible_but_is_not_processed_or_generated(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        application = create_or_get_application(session, duplicate_id)
        application.status = JobStatus.READY_FOR_FORM_SUBMISSION
    service = DesktopService()

    rows = service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    assert [row.id for row in rows] == [duplicate_id]
    assert rows[0].status == JobStatus.DUPLICATE_REVIEW
    assert rows[0].can_generate is False
    assert rows[0].can_send is False

    detail = service.get_job(duplicate_id)
    assert detail is not None
    assert detail.duplicate_candidate is not None
    assert detail.duplicate_candidate.id == root_id

    with session_scope() as session:
        assert duplicate_id not in {int(job.id) for job in list_pending_processing(session)}

    report = service.generate_applications([duplicate_id])
    assert report["generated"] == 0
    assert report["duplicate_review_ids"] == [duplicate_id]
    assert service.mark_job_sent(duplicate_id) is False

    mixin = ApplicationPersistenceMixin()
    with pytest.raises(DuplicateReviewRequiredError):
        mixin._reserve_application_id(ApplyReport(job_id=root_id, application_id=None))
    with pytest.raises(DuplicateReviewRequiredError):
        mixin._reserve_application_id(ApplyReport(job_id=duplicate_id, application_id=None))


def test_archived_only_duplicate_pair_is_not_shown_in_review_queue(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.archived_at = datetime.now(timezone.utc)
        root.status = JobStatus.ARCHIVED
        duplicate.archived_at = datetime.now(timezone.utc)
        duplicate.status = JobStatus.ARCHIVED

    assert DesktopService().list_jobs(status=JobStatus.DUPLICATE_REVIEW) == []


def test_duplicate_pair_with_one_active_offer_stays_in_review_queue(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        assert root is not None
        root.archived_at = datetime.now(timezone.utc)
        root.status = JobStatus.ARCHIVED

    rows = DesktopService().list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    assert [row.id for row in rows] == [duplicate_id]


def test_confirm_duplicate_keeps_active_offer_as_canonical(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        assert root is not None
        root.archived_at = datetime.now(timezone.utc)
        root.status = JobStatus.ARCHIVED

    assert DesktopService().resolve_duplicate(duplicate_id, same_offer=True)
    with session_scope() as session:
        active = session.get(Job, duplicate_id)
        archived = session.get(Job, root_id)
        assert active is not None
        assert archived is not None
        assert active.canonical_job_id is None
        assert active.duplicate_review_status == JobDuplicateStatus.CONFIRMED
        assert active.status == JobStatus.SCRAPED
        assert archived.canonical_job_id == duplicate_id
        assert archived.status == JobStatus.ARCHIVED
        assert canonical_job(session, root_id).id == duplicate_id


def test_active_offer_exposes_archived_alias_dossier_and_skips_generation(
    isolated_db, monkeypatch
) -> None:
    root_id, active_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        root.archived_at = datetime.now(timezone.utc)
        root.status = JobStatus.ARCHIVED
        app = create_or_get_application(session, root_id)
        app.status = JobStatus.ARCHIVED
        app.cv_docx_path = "/documents/127/CV.docx"
        app_id = app.id
        active = session.get(Job, active_id)
        active.analyzed_at = datetime.now(timezone.utc)
        active.shortlisted_at = datetime.now(timezone.utc)
        active.status = JobStatus.SHORTLISTED

    service = DesktopService()
    assert service.resolve_duplicate(active_id, same_offer=True)
    row = service.list_jobs(status=JobStatus.SHORTLISTED)[0]
    detail = service.get_job(active_id)
    for item in (row, detail):
        assert item.id == active_id
        assert item.status == JobStatus.SHORTLISTED
        assert item.application_id is None
        assert item.related_application_id == app_id
        assert item.can_generate is False
    assert service.shortlist_summary().ready_to_generate == 0
    assert service.shortlisted_generation_ids() == []
    assert service.get_application(detail.related_application_id).cv_docx_path.endswith("CV.docx")

    class NoGenerationPipeline:
        def apply_to(self, job_id):
            raise AssertionError("an existing duplicate dossier must not be regenerated")

    monkeypatch.setattr("smartapply.pipeline.Pipeline", NoGenerationPipeline)
    report = service.generate_applications([active_id])
    assert report["generated"] == 0
    assert report["skipped"] == 1
    assert report["failed"] == 0
    with session_scope() as session:
        app = session.get(Application, app_id)
        assert app.job_id == root_id
        assert app.status == JobStatus.ARCHIVED


def test_group_application_lookup_does_not_merge_pending_or_rejected_matches(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        app = create_or_get_application(session, root_id)
        assert application_ids_for_confirmed_groups(session, [root_id, duplicate_id]) == {
            root_id: app.id
        }
    assert DesktopService().resolve_duplicate(duplicate_id, same_offer=False)
    detail = DesktopService().get_job(duplicate_id)
    assert detail.related_application_id is None


def test_reject_duplicate_keeps_archived_offer_archived(isolated_db) -> None:
    _root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        duplicate.archived_at = datetime.now(timezone.utc)
        duplicate.status = JobStatus.ARCHIVED

    assert DesktopService().resolve_duplicate(duplicate_id, same_offer=False)
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.REJECTED
        assert duplicate.status == JobStatus.ARCHIVED
        assert duplicate.archived_at is not None


def test_rejecting_match_releases_two_independent_offers(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    service = DesktopService()

    assert service.resolve_duplicate(duplicate_id, same_offer=False)
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.REJECTED
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate_group_ids(session, duplicate_id) == {duplicate_id}
        assert application_for_duplicate_group(session, duplicate_id) is None
        assert root_id != duplicate_id

    with session_scope() as session:
        assert duplicate_id in {int(job.id) for job in list_pending_processing(session)}


@pytest.mark.parametrize("application_status", APPLICATION_STATUSES)
def test_confirmed_alias_blocks_against_every_application_status_including_archived(
    isolated_db,
    application_status: str,
) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        application = create_or_get_application(session, root_id)
        application.status = application_status
        root = session.get(Job, root_id)
        assert root is not None
        root.status = application_status
        if application_status == JobStatus.ARCHIVED:
            root.archived_at = datetime.now(timezone.utc)

    assert DesktopService().resolve_duplicate(duplicate_id, same_offer=True)
    with session_scope() as session:
        alias = session.get(Job, duplicate_id)
        assert alias is not None
        if application_status == JobStatus.ARCHIVED:
            # The active offer must remain canonical when the historical
            # offer is already archived.
            assert alias.canonical_job_id is None
            assert alias.status == JobStatus.SCRAPED
            assert canonical_job(session, root_id).id == duplicate_id
        else:
            assert alias.canonical_job_id == root_id
            assert alias.status == JobStatus.ARCHIVED
            assert canonical_job(session, duplicate_id).id == root_id
        assert alias.duplicate_review_status == JobDuplicateStatus.CONFIRMED
        grouped_application = application_for_duplicate_group(session, duplicate_id)
        assert grouped_application is not None
        assert grouped_application.status == application_status

    report = ApplyReport(job_id=duplicate_id, application_id=None)
    with pytest.raises(ApplicationAlreadyExistsError):
        ApplicationPersistenceMixin()._reserve_application_id(report)

    with session_scope() as session:
        assert session.query(Application).count() == 1
