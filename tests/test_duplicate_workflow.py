"""Regression tests for offer identity, duplicate review and application guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import permutations

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
    set_score,
    upsert_job,
)
from smartapply.dedup import Deduplicator
from smartapply.desktop.services import DesktopService
from smartapply.jobsearch.workflow import APPLICATION_STATUSES
from smartapply.offers import RawJob, make_external_id
from smartapply.pipeline.apply.persistence import ApplicationPersistenceMixin
from smartapply.pipeline.errors import ApplicationAlreadyExistsError, DuplicateReviewRequiredError
from smartapply.pipeline.ingest import _KnownJobIndex, collect_round_robin
from smartapply.pipeline.ingestor import Ingestor, _is_direct_offer_url
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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/jobs/search/?keywords=data", False),
        ("https://www.linkedin.com/jobs/collections/recommended/", False),
        ("https://www.linkedin.com/jobs/view/4464028174", True),
        (
            "https://fr.linkedin.com/jobs/view/data-scientist-acme-4464028174?position=2",
            True,
        ),
        ("https://example.test/jobs", False),
        ("https://example.test/jobs/search?q=data", False),
        ("https://example.test/jobs/data-scientist-42", True),
        ("https://candidat.francetravail.fr/offres/recherche/detail/123ABC", True),
    ],
)
def test_direct_offer_url_excludes_shared_listing_pages(url, expected) -> None:
    assert _is_direct_offer_url(url) is expected


def test_exact_title_company_location_does_not_override_different_descriptions(isolated_db) -> None:
    ingestor = Ingestor()
    first = ingestor._persist(
        "serpapi", [_raw("serpapi:exact-1", "https://acme.test/jobs/exact-1")]
    )
    second_raw = _raw("serpapi:exact-2", "https://acme.test/jobs/exact-2").model_copy(
        update={"description": "Description réécrite par une autre source."}
    )

    second = ingestor._persist("serpapi", [second_raw])

    assert second.duplicate_review_ids == []
    assert second.persisted == 1
    with session_scope() as session:
        original = session.get(Job, first.job_ids[0])
        other = session.get(Job, second.job_ids[0])
        assert other is not None and original is not None
        assert other.duplicate_review_status == JobDuplicateStatus.NONE
        assert other.canonical_job_id is None
        assert original.canonical_job_id is None
        assert original.status == JobStatus.SCRAPED


def _create_review_triplet() -> tuple[int, int, int]:
    root_id, second_id = _create_review_pair()
    third = Ingestor()._persist(
        "serpapi", [_raw("serpapi:source-3", "https://acme.test/jobs/3")]
    )
    assert third.duplicate_review_ids == third.job_ids
    return root_id, second_id, third.job_ids[0]


def _create_review_chain() -> tuple[int, int, int]:
    first = "python pandas numpy statistics machine learning"
    last = "java spring kotlin android mobile applications"
    ids = []
    for index, description in enumerate((first, first + " " + last, last)):
        raw = _raw(f"chain:{index}", f"https://acme.test/jobs/chain-{index}").model_copy(
            update={"description": description}
        )
        report = Ingestor()._persist("serpapi", [raw])
        ids.append(report.job_ids[0])
    with session_scope() as session:
        for job_id in ids:
            session.get(Job, job_id).filtered_at = datetime.now(timezone.utc)
        assert session.get(Job, ids[1]).possible_duplicate_of_id == ids[0]
        assert session.get(Job, ids[2]).possible_duplicate_of_id == ids[1]
    return tuple(ids)


@pytest.mark.parametrize("keep_middle", [False, True])
@pytest.mark.parametrize("accept_upstream", [False, True])
def test_chain_retains_upstream_decision_after_downstream_confirmation(
    isolated_db, keep_middle, accept_upstream
) -> None:
    first_id, middle_id, last_id = _create_review_chain()
    service = DesktopService()
    with session_scope() as session:
        middle = session.get(Job, middle_id)
        metadata = (middle.duplicate_match_type, middle.duplicate_confidence)
        if keep_middle:
            middle.published_date = datetime(2030, 1, 1, tzinfo=timezone.utc)
        app = create_or_get_application(session, middle_id)
        app.status = JobStatus.SENT
        app.cv_docx_path = "/documents/chain/CV.docx"
        app_id = app.id

    assert service.resolve_duplicate(last_id, same_offer=True)
    winner_id = middle_id if keep_middle else last_id
    rows = service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    assert [row.id for row in rows] == [winner_id]
    assert service.get_job(winner_id).duplicate_candidate.id == first_id
    with session_scope() as session:
        winner = session.get(Job, winner_id)
        assert winner.possible_duplicate_of_id == first_id
        assert (winner.duplicate_match_type, winner.duplicate_confidence) == metadata
        assert canonical_job(session, middle_id).id == canonical_job(session, last_id).id == winner_id
        assert canonical_job(session, first_id).id == first_id
        assert session.get(Application, app_id).job_id == winner_id

    for job_id in (first_id, middle_id, last_id):
        with pytest.raises(DuplicateReviewRequiredError):
            ApplicationPersistenceMixin()._reserve_application_id(
                ApplyReport(job_id=job_id, application_id=None)
            )

    assert service.resolve_duplicate(winner_id, same_offer=accept_upstream)
    assert service.list_jobs(status=JobStatus.DUPLICATE_REVIEW) == []
    with session_scope() as session:
        assert canonical_job(session, middle_id).id == canonical_job(session, last_id).id == winner_id
        assert canonical_job(session, first_id).id == (winner_id if accept_upstream else first_id)
        assert session.get(Application, app_id).job_id == winner_id
        assert session.query(Application).count() == 1
    assert service.get_job(winner_id).status == JobStatus.SENT
    assert service.get_job(winner_id).can_send is False


@pytest.mark.parametrize("order", list(permutations((1, 2, 3))))
def test_four_offer_chain_preserves_all_decisions_and_aliases(isolated_db, order) -> None:
    ids = list(_create_review_chain())
    with session_scope() as session:
        last = upsert_job(
            session, external_id="chain:3", title="Data Scientist", company="Acme SAS",
            description=_DESCRIPTION, source="serpapi", filtered_at=datetime.now(timezone.utc),
        )
        last.possible_duplicate_of_id = ids[-1]
        last.duplicate_review_status = JobDuplicateStatus.PENDING
        ids.append(last.id)
        for index, job_id in enumerate(ids):
            session.get(Job, job_id).published_date = datetime(2025, 1, index + 1, tzinfo=timezone.utc)
        app = create_or_get_application(session, ids[0])
        app.status = JobStatus.SENT
        app.cv_docx_path = "/documents/chain/CV.docx"
        app_id = app.id
    for step, index in enumerate(order):
        with session_scope() as session:
            target_id = canonical_job(session, ids[index]).id
        assert DesktopService().resolve_duplicate(target_id, same_offer=True)
        with session_scope() as session:
            remaining = session.query(Job).filter_by(duplicate_review_status=JobDuplicateStatus.PENDING).count()
            assert remaining == 2 - step
    with session_scope() as session:
        assert {canonical_job(session, job_id).id for job_id in ids} == {ids[-1]}
        assert session.get(Application, app_id).job_id == ids[-1]
        assert session.query(Application).count() == 1


def test_confirm_does_not_take_dossier_from_unconfirmed_sibling(isolated_db) -> None:
    root_id, second_id, third_id = _create_review_triplet()
    with session_scope() as session:
        application = create_or_get_application(session, third_id)
        application.status = JobStatus.SENT
        application.cv_docx_path = "/documents/third/CV.docx"
        app_id = application.id

    service = DesktopService()
    assert service.resolve_duplicate(second_id, same_offer=True)
    with session_scope() as session:
        assert session.get(Application, app_id).job_id == third_id
        assert session.get(Job, third_id).duplicate_review_status == JobDuplicateStatus.PENDING
        assert canonical_job(session, root_id).id == second_id
    assert service.resolve_duplicate(third_id, same_offer=False)
    assert service.get_job(second_id).application_id is None
    assert service.get_job(second_id).related_application_id is None
    assert service.get_job(third_id).application_id == app_id


@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("publication_dates", [False, True])
def test_latest_offer_and_dossier_do_not_depend_on_confirmation_order(
    isolated_db, reverse_order, publication_dates
) -> None:
    ids = _create_review_triplet()
    date = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        for index, job_id in enumerate(ids):
            job = session.get(Job, job_id)
            job.scraped_at = date + timedelta(days=(2 - index if publication_dates else index))
            if publication_dates:
                job.published_date = date + timedelta(days=index)
        app = create_or_get_application(session, ids[0])
        app.status = JobStatus.SENT
        app.form_submitted_at = date
        app.cv_docx_path = "/documents/original/CV.docx"
        app_id = app.id
    service = DesktopService()
    for job_id in (reversed(ids[1:]) if reverse_order else ids[1:]):
        assert service.resolve_duplicate(job_id, same_offer=True)
    with session_scope() as session:
        assert {canonical_job(session, job_id).id for job_id in ids} == {ids[2]}
        assert session.get(Application, app_id).job_id == ids[2]
        assert session.query(Application).count() == 1
    assert service.get_job(ids[2]).status == JobStatus.SENT
    assert not service.get_job(ids[2]).can_send


def test_confirmation_with_two_dossiers_preserves_both_and_stays_pending(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        first = create_or_get_application(session, root_id)
        second = create_or_get_application(session, duplicate_id)
    with pytest.raises(ValueError, match="plusieurs dossiers"):
        DesktopService().resolve_duplicate(duplicate_id, same_offer=True)
    with session_scope() as session:
        assert session.get(Application, first.id).job_id == root_id
        assert session.get(Application, second.id).job_id == duplicate_id
        assert session.get(Job, duplicate_id).duplicate_review_status == JobDuplicateStatus.PENDING


@pytest.mark.parametrize("tracking_status", [JobStatus.SENT, JobStatus.INTERVIEW, JobStatus.REJECTED])
def test_republication_keeps_sent_dossier_and_archived_history(isolated_db, tracking_status) -> None:
    from smartapply.database.repository import add_document

    root_id, new_id = _create_review_pair()
    sent_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        historical = upsert_job(
            session, external_id="history:archived", title="Data Scientist", company="Acme SAS",
            description=_DESCRIPTION, source="linkedin", status=JobStatus.ARCHIVED,
            archived_at=sent_at, canonical_job_id=root_id,
            duplicate_review_status=JobDuplicateStatus.CONFIRMED,
        )
        sent = create_or_get_application(session, root_id)
        sent.status = tracking_status
        sent.form_submitted_at = sent_at
        sent.updated_at = sent_at
        sent.notes = "Candidature envoyée"
        sent.cv_docx_path = "/documents/49/CV.docx"
        archived = create_or_get_application(session, historical.id)
        archived.status = JobStatus.ARCHIVED
        archived.updated_at = sent_at + timedelta(days=1)
        archived.notes = "Ancien dossier conservé"
        archived.cv_docx_path = "/documents/80/CV.docx"
        add_document(session, sent.id, doc_type="cv_docx", path=sent.cv_docx_path)
        add_document(session, archived.id, doc_type="cv_docx", path=archived.cv_docx_path)
        sent_id, archived_id, historical_id = sent.id, archived.id, historical.id

    service = DesktopService()
    candidate = service.get_job(new_id).duplicate_candidate
    assert candidate.application_id == sent_id
    assert candidate.application_status == tracking_status
    assert service.resolve_duplicate(new_id, same_offer=True)
    with session_scope() as session:
        sent = session.get(Application, sent_id)
        archived = session.get(Application, archived_id)
        assert session.query(Application).count() == 2
        assert sent.job_id == new_id
        assert sent.status == tracking_status
        assert sent.updated_at.replace(tzinfo=timezone.utc) == sent_at
        assert sent.form_submitted_at.replace(tzinfo=timezone.utc) == sent_at
        assert sent.notes == "Candidature envoyée"
        assert len(sent.documents) == 1
        assert archived.job_id == historical_id
        assert archived.status == JobStatus.ARCHIVED
        assert archived.notes == "Ancien dossier conservé"
        assert len(archived.documents) == 1
        assert canonical_job(session, historical_id).id == new_id
        assert application_for_duplicate_group(session, historical_id).id == sent_id
        assert application_ids_for_confirmed_groups(session, [root_id, new_id, historical_id]) == {
            root_id: sent_id, new_id: sent_id, historical_id: sent_id,
        }
    detail = service.get_job(new_id)
    assert detail.application_id == sent_id
    assert detail.status == tracking_status
    assert not detail.can_send
    assert not detail.can_generate
    for job_id in (root_id, new_id, historical_id):
        with pytest.raises(ApplicationAlreadyExistsError) as error:
            ApplicationPersistenceMixin()._reserve_application_id(
                ApplyReport(job_id=job_id, application_id=None)
            )
        assert error.value.application_id == sent_id


def test_duplicate_badge_does_not_use_unconfirmed_siblings_dossier(isolated_db) -> None:
    root_id, second_id, third_id = _create_review_triplet()
    with session_scope() as session:
        app = create_or_get_application(session, third_id)
        app.status = JobStatus.SENT
    candidate = DesktopService().get_job(second_id).duplicate_candidate
    assert candidate.id == root_id
    assert candidate.application_id is None


@pytest.mark.parametrize("restore_new", [False, True])
def test_hidden_archived_pair_can_be_restored_for_review(isolated_db, restore_new) -> None:
    ids = _create_review_pair()
    service = DesktopService()
    for job_id in ids:
        service.archive_job(job_id)
    assert service.list_jobs(status=JobStatus.DUPLICATE_REVIEW) == []
    assert set(ids).issubset({row.id for row in service.list_jobs(status=JobStatus.ARCHIVED)})
    assert service.get_job(ids[1]).status == JobStatus.ARCHIVED

    report = service.rescue_job(ids[int(restore_new)])
    assert report["rescued"] == 1
    assert report["failed"] == report["generated"] == 0
    assert report["duplicate_review_ids"] == [ids[1]]
    assert [row.id for row in service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)] == [ids[1]]
    with session_scope() as session:
        assert session.get(Job, ids[1]).duplicate_review_status == JobDuplicateStatus.PENDING
        assert session.query(Application).count() == 0


def test_review_limit_counts_visible_pairs_only(isolated_db) -> None:
    root_id, hidden_id = _create_review_pair()
    service = DesktopService()
    service.archive_job(root_id)
    service.archive_job(hidden_id)
    third = Ingestor()._persist(
        "serpapi", [_raw("serpapi:visible", "https://acme.test/jobs/visible")]
    )
    visible_id = third.job_ids[0]
    with session_scope() as session:
        session.get(Job, hidden_id).duplicate_confidence = 1
        session.get(Job, visible_id).duplicate_confidence = 0.8
    assert [row.id for row in service.list_jobs(
        status=JobStatus.DUPLICATE_REVIEW, sort_key="duplicate_confidence", limit=1
    )] == [visible_id]


def test_review_checks_canonical_offer_after_other_pair_is_confirmed(isolated_db) -> None:
    root_id, second_id, third_id = _create_review_triplet()
    service = DesktopService()
    service.archive_job(third_id)
    service.resolve_duplicate(second_id, same_offer=True)
    with session_scope() as session:
        assert session.get(Job, root_id).archived_at is not None
        assert session.get(Job, third_id).possible_duplicate_of_id == root_id
    assert [row.id for row in service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)] == [third_id]
    assert service.get_job(third_id).duplicate_candidate.id == second_id


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


def test_generic_offer_url_requires_review_before_archiving(isolated_db) -> None:
    ingestor = Ingestor()
    first = ingestor._persist(
        "serpapi",
        [_raw("serpapi:old-id", "https://acme.test/jobs/canonical", source="serpapi")],
    )
    second = ingestor._persist(
        "linkedin",
        [_raw("linkedin:new-id", "https://acme.test/jobs/canonical", source="linkedin")],
    )

    assert second.aliases_created == 0
    assert second.duplicate_review_ids == second.job_ids
    with session_scope() as session:
        alias = session.query(Job).filter(Job.external_id == "linkedin:new-id").one()
        assert alias is not None
        assert alias.canonical_job_id is None
        assert alias.possible_duplicate_of_id == first.job_ids[0]
        assert alias.duplicate_review_status == JobDuplicateStatus.PENDING
        assert alias.status == JobStatus.SCRAPED
        assert alias.shortlisted_at is None


def test_linkedin_slug_and_numeric_urls_share_one_strong_identity(isolated_db) -> None:
    ingestor = Ingestor()
    first = ingestor._persist(
        "serpapi",
        [
            _raw(
                "serpapi:linkedin-numeric",
                "https://www.linkedin.com/jobs/view/4464028174?position=1",
                source="serpapi",
            )
        ],
    )
    second = ingestor._persist(
        "linkedin",
        [
            _raw(
                "linkedin:slug",
                "https://fr.linkedin.com/jobs/view/data-scientist-acme-4464028174"
                "?position=2&utm_source=search",
                source="linkedin",
            )
        ],
    )

    assert second.aliases_created == 1
    assert second.duplicate_review_ids == []
    with session_scope() as session:
        alias = session.query(Job).filter(Job.external_id == "linkedin:slug").one()
        assert alias.canonical_job_id == first.job_ids[0]
        assert alias.duplicate_review_status == JobDuplicateStatus.CONFIRMED


def test_same_direct_offer_url_over_one_month_apart_stays_distinct(isolated_db) -> None:
    published = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ingestor = Ingestor()
    first = _raw(
        "serpapi:old-republication",
        "https://acme.test/jobs/republication",
        source="serpapi",
    ).model_copy(update={"published_date": published})
    later = _raw(
        "linkedin:new-republication",
        "https://acme.test/jobs/republication",
        source="linkedin",
    ).model_copy(update={"published_date": published + timedelta(days=31)})

    first_report = ingestor._persist("serpapi", [first])
    later_report = ingestor._persist("linkedin", [later])

    assert later_report.aliases_created == 0
    assert later_report.duplicate_review_ids == []
    with session_scope() as session:
        old_job = session.get(Job, first_report.job_ids[0])
        new_job = session.get(Job, later_report.job_ids[0])
        assert old_job is not None
        assert new_job is not None
        assert old_job.canonical_job_id is None
        assert new_job.canonical_job_id is None
        assert new_job.duplicate_review_status == JobDuplicateStatus.NONE


def test_exact_url_alias_cannot_bridge_republications_over_one_month(isolated_db) -> None:
    published = datetime(2026, 8, 1, tzinfo=timezone.utc)
    url = "https://www.linkedin.com/jobs/view/4464028174"
    ingestor = Ingestor()

    first = ingestor._persist(
        "serpapi",
        [
            _raw("serpapi:chain-day-1", url, source="serpapi").model_copy(
                update={"published_date": published}
            )
        ],
    )
    middle = ingestor._persist(
        "linkedin",
        [
            _raw("linkedin:chain-day-20", url, source="linkedin").model_copy(
                update={"published_date": published + timedelta(days=19)}
            )
        ],
    )
    later = ingestor._persist(
        "linkedin",
        [
            _raw("linkedin:chain-day-45", url, source="linkedin").model_copy(
                update={"published_date": published + timedelta(days=44)}
            )
        ],
    )

    assert middle.aliases_created == 1
    assert later.aliases_created == 0
    assert later.duplicate_review_ids == []
    with session_scope() as session:
        later_job = session.get(Job, later.job_ids[0])
        assert later_job is not None
        assert later_job.canonical_job_id is None
        assert later_job.duplicate_review_status == JobDuplicateStatus.NONE
        assert canonical_job(session, later_job.id).id != first.job_ids[0]


def test_backfill_releases_pending_duplicate_over_one_month_apart(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    published = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.published_date = published
        duplicate.published_date = published + timedelta(days=31)

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.NONE
        assert duplicate.duplicate_match_type is None
        assert duplicate.duplicate_confidence is None


def test_backfill_relink_checks_aliases_outside_the_company_bucket(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    published = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.published_date = published
        duplicate.published_date = published + timedelta(days=1)
        alias = upsert_job(
            session,
            external_id="legacy:old-alias-other-company",
            title=root.title,
            company="Entreprise non communiquée",
            location=root.location,
            description=root.description,
            application_url="https://acme.test/jobs/old-alias",
            source="linkedin",
            published_date=published - timedelta(days=61),
        )
        alias.canonical_job_id = root_id
        alias.duplicate_review_status = JobDuplicateStatus.CONFIRMED
        alias.duplicate_match_type = "exact_url"
        alias.duplicate_confidence = 1.0
        alias.archived_at = datetime.now(timezone.utc)
        alias.status = JobStatus.ARCHIVED

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.NONE


def test_backfill_strong_url_still_respects_full_group_time_window(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    published = datetime(2026, 10, 1, tzinfo=timezone.utc)
    direct_url = "https://acme.test/jobs/shared-strong-identity"
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.published_date = published
        duplicate.published_date = published + timedelta(days=1)
        root.application_url = duplicate.application_url = direct_url
        alias = upsert_job(
            session,
            external_id="legacy:old-strong-alias",
            title=root.title,
            company="Entreprise non communiquée",
            location=root.location,
            description=root.description,
            application_url=direct_url,
            source="linkedin",
            published_date=published - timedelta(days=61),
        )
        alias.canonical_job_id = root_id
        alias.duplicate_review_status = JobDuplicateStatus.CONFIRMED
        alias.duplicate_match_type = "exact_url"
        alias.duplicate_confidence = 1.0
        alias.archived_at = datetime.now(timezone.utc)
        alias.status = JobStatus.ARCHIVED

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.NONE


def test_backfill_releases_anonymous_fuzzy_pair_without_touching_history(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    analyzed_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.company = duplicate.company = "Entreprise non communiquée"
        duplicate.analyzed_at = analyzed_at
        duplicate.source_data = {"legacy": "preserve"}
        application = create_or_get_application(session, duplicate_id)
        application.status = JobStatus.ARCHIVED
        application.notes = "Historique à conserver"
        application_id = application.id

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        application = session.get(Application, application_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.NONE
        assert duplicate.duplicate_match_type is None
        assert duplicate.duplicate_confidence is None
        assert duplicate.analyzed_at.replace(tzinfo=timezone.utc) == analyzed_at
        assert duplicate.source_data == {"legacy": "preserve"}
        assert application is not None
        assert application.job_id == duplicate_id
        assert application.status == JobStatus.ARCHIVED
        assert application.notes == "Historique à conserver"


def test_backfill_releases_fuzzy_pair_in_different_precise_cities(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.location = "75 - Paris"
        root.source_data = {"lieuTravail": {"commune": "75056"}}
        duplicate.location = "69 - Lyon"
        duplicate.source_data = {"lieuTravail": {"commune": "69123"}}

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.NONE
        assert duplicate.duplicate_match_type is None
        assert duplicate.duplicate_confidence is None


def test_backfill_caps_existing_fuzzy_confidence_below_certainty(isolated_db) -> None:
    _root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        duplicate.duplicate_confidence = 1.0

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.PENDING
        assert duplicate.duplicate_confidence == 0.99


def test_backfill_recomputes_pending_confidence_with_precise_location(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.location = "75 - Paris"
        duplicate.location = "Paris, Île-de-France, France"
        duplicate.description = (
            "Nous recherchons un Data Scientist pour développer des pipelines de données "
            "et des modèles Python et PyTorch en environnement de production."
        )
        expected = Deduplicator().find_probable_duplicate(duplicate, [root])
        assert expected is not None
        assert expected.location_score == 100.0
        duplicate.duplicate_confidence = 0.5
        expected_confidence = expected.confidence

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.PENDING
        assert duplicate.duplicate_confidence == expected_confidence


def test_backfill_recomputes_fuzzy_confidence_from_confirmed_alias(isolated_db) -> None:
    published = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        root = upsert_job(
            session,
            external_id="legacy:component-root",
            title="Platform Engineer",
            company="Acme SAS",
            location="Paris",
            description="Infrastructure cloud Kubernetes et observabilité.",
            application_url="https://acme.test/jobs/component-root",
            source="serpapi",
            published_date=published,
        )
        alias = upsert_job(
            session,
            external_id="legacy:component-alias",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/jobs/component-alias",
            source="linkedin",
            published_date=published,
        )
        alias.canonical_job_id = root.id
        alias.duplicate_review_status = JobDuplicateStatus.CONFIRMED
        alias.duplicate_match_type = "exact_url"
        alias.duplicate_confidence = 1.0
        alias.status = JobStatus.ARCHIVED
        target = upsert_job(
            session,
            external_id="legacy:component-target",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/jobs/component-target",
            source="francetravail",
            published_date=published,
        )
        target.possible_duplicate_of_id = root.id
        target.duplicate_review_status = JobDuplicateStatus.PENDING
        target.duplicate_match_type = "fuzzy"
        target.duplicate_confidence = 0.5
        expected = Deduplicator().find_probable_duplicate(target, [alias])
        assert expected is not None
        root_id = int(root.id)
        target_id = int(target.id)
        expected_confidence = expected.confidence

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        target = session.get(Job, target_id)
        assert target is not None
        assert target.possible_duplicate_of_id == root_id
        assert target.duplicate_review_status == JobDuplicateStatus.PENDING
        assert target.duplicate_confidence == expected_confidence


def test_backfill_later_pending_member_does_not_inflate_reference_confidence(isolated_db) -> None:
    published = datetime(2026, 10, 1, tzinfo=timezone.utc)
    target_description = _DESCRIPTION
    root_description = (
        "Nous recherchons un Data Scientist pour développer des modèles Python "
        "dans une plateforme cloud et maintenir les services analytiques."
    )
    with session_scope() as session:
        root = upsert_job(
            session,
            external_id="legacy:growing-root",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=root_description,
            application_url="https://acme.test/jobs/growing-root",
            source="serpapi",
            published_date=published,
        )
        target = upsert_job(
            session,
            external_id="legacy:growing-target",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=target_description,
            application_url="https://acme.test/jobs/growing-target",
            source="linkedin",
            published_date=published,
        )
        initial_match = Deduplicator().find_probable_duplicate(target, [root])
        assert initial_match is not None
        expected_confidence = initial_match.confidence
        target.possible_duplicate_of_id = root.id
        target.duplicate_review_status = JobDuplicateStatus.PENDING
        target.duplicate_match_type = "fuzzy"
        target.duplicate_confidence = initial_match.confidence
        later_member = upsert_job(
            session,
            external_id="legacy:growing-later-member",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=target_description,
            application_url="https://acme.test/jobs/growing-later-member",
            source="francetravail",
            published_date=published,
        )
        target_id = int(target.id)
        later_member_id = int(later_member.id)

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        target = session.get(Job, target_id)
        later_member = session.get(Job, later_member_id)
        assert target is not None
        assert later_member is not None
        assert target.duplicate_confidence == expected_confidence
        assert later_member.duplicate_review_status == JobDuplicateStatus.PENDING
        assert later_member.possible_duplicate_of_id == target_id


@pytest.mark.parametrize("sibling_location", ["Paris", "Lyon"])
def test_backfill_pending_siblings_cannot_prove_a_match_to_unrelated_reference(
    isolated_db, sibling_location
) -> None:
    root_id, target_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        target = session.get(Job, target_id)
        root.title = "Comptable fournisseurs"
        root.description = root.cleaned_description = (
            "Gestion des factures, clôtures et rapprochements bancaires."
        )
        target.location = ""
        target.duplicate_confidence = 0.5
        sibling = upsert_job(
            session,
            external_id="legacy:unsupported-pending-sibling",
            title="Data Scientist",
            company="Acme SAS",
            location=sibling_location,
            description=_DESCRIPTION,
            application_url="https://acme.test/jobs/unsupported-pending-sibling",
            source="linkedin",
        )
        sibling.possible_duplicate_of_id = root_id
        sibling.duplicate_review_status = JobDuplicateStatus.PENDING
        sibling.duplicate_match_type = "fuzzy"
        sibling.duplicate_confidence = 0.5
        sibling_id = int(sibling.id)
        application = create_or_get_application(session, root_id)
        application.status = JobStatus.SENT
        application.notes = "Dossier envoyé à conserver"
        application.cv_docx_path = "/documents/original/CV.docx"
        application_id = int(application.id)

    assert backfill_duplicate_reviews() == 2
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        target = session.get(Job, target_id)
        sibling = session.get(Job, sibling_id)
        application = session.get(Application, application_id)
        assert target.duplicate_review_status == JobDuplicateStatus.NONE
        assert target.possible_duplicate_of_id is None
        assert sibling.duplicate_review_status == JobDuplicateStatus.PENDING
        assert sibling.possible_duplicate_of_id == target_id
        assert application_for_duplicate_group(session, target_id) is None
        assert application_for_duplicate_group(session, sibling_id) is None
        assert application.job_id == root_id
        assert application.status == JobStatus.SENT
        assert application.notes == "Dossier envoyé à conserver"
        assert application.cv_docx_path == "/documents/original/CV.docx"


def test_backfill_strong_identity_from_pending_sibling_does_not_protect_wrong_reference(
    isolated_db,
) -> None:
    root_id, target_id = _create_review_pair()
    shared_url = "https://acme.test/jobs/shared-pending-identity"
    with session_scope() as session:
        root = session.get(Job, root_id)
        target = session.get(Job, target_id)
        root.company = target.company = "Entreprise non communiquée"
        target.application_url = shared_url
        sibling = upsert_job(
            session,
            external_id="legacy:strong-pending-sibling",
            title=target.title,
            company=target.company,
            location=target.location,
            description=_DESCRIPTION,
            application_url=shared_url,
            source="linkedin",
        )
        sibling.possible_duplicate_of_id = root_id
        sibling.duplicate_review_status = JobDuplicateStatus.PENDING
        sibling.duplicate_match_type = "fuzzy"
        sibling.duplicate_confidence = 0.5
        sibling_id = int(sibling.id)

    assert backfill_duplicate_reviews() == 2
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        for job_id in (target_id, sibling_id):
            job = session.get(Job, job_id)
            assert job.possible_duplicate_of_id is None
            assert job.duplicate_review_status == JobDuplicateStatus.NONE
            assert job.duplicate_confidence is None


@pytest.mark.parametrize("conflict", ["location", "date"])
def test_backfill_new_link_respects_full_incoming_pending_group(isolated_db, conflict) -> None:
    published = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        root = upsert_job(
            session,
            external_id="legacy:join-paris-root",
            title="Data Scientist",
            company="Acme SAS",
            location="Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/jobs/join-paris-root",
            source="serpapi",
            published_date=published,
        )
        bridge = upsert_job(
            session,
            external_id="legacy:join-unknown-bridge",
            title=root.title,
            company=root.company,
            location="",
            description=_DESCRIPTION,
            application_url="https://acme.test/jobs/join-unknown-bridge",
            source="linkedin",
            published_date=published + timedelta(days=20) if conflict == "date" else published,
        )
        child = upsert_job(
            session,
            external_id="legacy:join-lyon-child",
            title=root.title,
            company=root.company,
            location="Lyon" if conflict == "location" else "Paris",
            description=_DESCRIPTION,
            application_url="https://acme.test/jobs/join-lyon-child",
            source="francetravail",
            published_date=published + timedelta(days=40) if conflict == "date" else published,
        )
        child.possible_duplicate_of_id = bridge.id
        child.duplicate_review_status = JobDuplicateStatus.PENDING
        child.duplicate_match_type = "fuzzy"
        match = Deduplicator().find_probable_duplicate(child, [bridge])
        assert match is not None
        child.duplicate_confidence = match.confidence
        bridge_id, child_id = int(bridge.id), int(child.id)

    assert backfill_duplicate_reviews() == 0
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        bridge = session.get(Job, bridge_id)
        child = session.get(Job, child_id)
        assert bridge.possible_duplicate_of_id is None
        assert bridge.duplicate_review_status == JobDuplicateStatus.NONE
        assert child.possible_duplicate_of_id == bridge_id
        assert child.duplicate_review_status == JobDuplicateStatus.PENDING


def test_backfill_finds_strong_identity_in_confirmed_alias(isolated_db) -> None:
    published = datetime(2026, 10, 1, tzinfo=timezone.utc)
    shared_url = "https://acme.test/jobs/identity-through-alias"
    with session_scope() as session:
        root = upsert_job(
            session,
            external_id="legacy:strong-component-root",
            title="Platform Engineer",
            company="Entreprise non communiquée",
            location="Paris",
            description="Infrastructure cloud et Kubernetes.",
            application_url="https://acme.test/jobs/different-root-url",
            source="serpapi",
            published_date=published,
        )
        alias = upsert_job(
            session,
            external_id="legacy:strong-component-alias",
            title="Data Scientist",
            company="Entreprise non communiquée",
            location="Paris",
            description=_DESCRIPTION,
            application_url=shared_url,
            source="linkedin",
            published_date=published,
        )
        alias.canonical_job_id = root.id
        alias.duplicate_review_status = JobDuplicateStatus.CONFIRMED
        alias.duplicate_match_type = "exact_url"
        alias.duplicate_confidence = 1.0
        alias.status = JobStatus.ARCHIVED
        target = upsert_job(
            session,
            external_id="legacy:strong-component-target",
            title="Data Scientist",
            company="Entreprise non communiquée",
            location="Lyon",
            description=_DESCRIPTION,
            application_url=shared_url,
            source="francetravail",
            published_date=published,
        )
        target.possible_duplicate_of_id = root.id
        target.duplicate_review_status = JobDuplicateStatus.PENDING
        target.duplicate_match_type = "exact_url"
        target.duplicate_confidence = 1.0
        root_id = int(root.id)
        target_id = int(target.id)

    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        target = session.get(Job, target_id)
        assert target is not None
        assert target.possible_duplicate_of_id == root_id
        assert target.duplicate_review_status == JobDuplicateStatus.PENDING
        assert target.duplicate_match_type == "exact_url"
        assert target.duplicate_confidence == 1.0


def test_backfill_releases_fuzzy_review_that_matches_no_group_member(isolated_db) -> None:
    _root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        duplicate.title = "Comptable fournisseurs"
        duplicate.description = "Gestion des factures, clôtures et rapprochements bancaires."

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.NONE


def test_backfill_reclassifies_exact_url_review_without_strong_identity(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        duplicate.duplicate_match_type = "exact_url"
        duplicate.duplicate_confidence = 1.0

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id == root_id
        assert duplicate.duplicate_review_status == JobDuplicateStatus.PENDING
        assert duplicate.duplicate_match_type == "fuzzy"
        assert duplicate.duplicate_confidence == 0.99


@pytest.mark.parametrize(
    "review_status",
    [JobDuplicateStatus.CONFIRMED, JobDuplicateStatus.REJECTED],
)
def test_location_backfill_preserves_resolved_review_and_history(
    isolated_db,
    review_status,
) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.location = "Paris"
        duplicate.location = "Lyon"
        duplicate.duplicate_review_status = review_status
        duplicate.possible_duplicate_of_id = None
        duplicate.canonical_job_id = (
            root_id if review_status == JobDuplicateStatus.CONFIRMED else None
        )
        duplicate.duplicate_confidence = 0.87
        application = create_or_get_application(session, duplicate_id)
        application.notes = "Historique résolu à préserver"
        application_id = application.id

    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        application = session.get(Application, application_id)
        assert duplicate is not None
        assert duplicate.duplicate_review_status == review_status
        assert duplicate.duplicate_confidence == 0.87
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.canonical_job_id == (
            root_id if review_status == JobDuplicateStatus.CONFIRMED else None
        )
        assert application is not None
        assert application.notes == "Historique résolu à préserver"


def test_backfill_preserves_mislabeled_fuzzy_pair_with_same_direct_url(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    direct_url = (
        "https://fr.linkedin.com/jobs/view/data-scientist-acme-4464028174"
        "?position=2&utm_source=legacy"
    )
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.company = duplicate.company = "Entreprise non communiquée"
        root.location = "75 - Paris"
        duplicate.location = "69 - Lyon"
        root.application_url = "https://www.linkedin.com/jobs/view/4464028174?position=1"
        duplicate.application_url = direct_url
        duplicate.duplicate_confidence = 1.0

    assert backfill_duplicate_reviews() == 1
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id == root_id
        assert duplicate.duplicate_review_status == JobDuplicateStatus.PENDING
        assert duplicate.duplicate_match_type == "fuzzy"
        assert duplicate.duplicate_confidence == 0.99


def test_backfill_preserves_mislabeled_fuzzy_pair_with_same_france_travail_id(
    isolated_db,
) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.company = duplicate.company = "Entreprise non communiquée"
        root.source = duplicate.source = "francetravail"
        root.location = "75 - Paris"
        duplicate.location = "69 - Lyon"
        root.source_data = {
            "id": "213HYVJ",
            "lieuTravail": {"commune": "75056"},
        }
        duplicate.source_data = {
            "id": "213HYVJ",
            "lieuTravail": {"commune": "69123"},
        }

    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id == root_id
        assert duplicate.duplicate_review_status == JobDuplicateStatus.PENDING
        assert duplicate.duplicate_match_type == "fuzzy"


def test_backfill_does_not_treat_shared_search_page_as_strong_identity(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        root = session.get(Job, root_id)
        duplicate = session.get(Job, duplicate_id)
        assert root is not None
        assert duplicate is not None
        root.company = duplicate.company = "Entreprise non communiquée"
        root.application_url = duplicate.application_url = (
            "https://www.linkedin.com/jobs/search/?keywords=data"
        )

    assert backfill_duplicate_reviews() == 1
    with session_scope() as session:
        duplicate = session.get(Job, duplicate_id)
        assert duplicate is not None
        assert duplicate.possible_duplicate_of_id is None
        assert duplicate.duplicate_review_status == JobDuplicateStatus.NONE


def test_manual_direct_url_with_different_label_requires_review(isolated_db) -> None:
    ingestor = Ingestor()
    first = ingestor._persist(
        "serpapi",
        [_raw("serpapi:existing", "https://acme.test/jobs/canonical")],
    )
    manual = _raw(
        "manual:new",
        "https://acme.test/jobs/canonical",
        title="ML Platform Engineer",
        source="manual",
    ).model_copy(update={"company": "Acme France"})

    report = ingestor._persist("manual", [manual])

    assert report.aliases_created == 0
    assert report.duplicate_review_ids == report.job_ids
    with session_scope() as session:
        reviewed = session.get(Job, report.job_ids[0])
        assert reviewed is not None
        assert reviewed.possible_duplicate_of_id == first.job_ids[0]
        assert reviewed.duplicate_review_status == JobDuplicateStatus.PENDING
        assert reviewed.duplicate_match_type == "exact_url"
        assert reviewed.duplicate_confidence == 1.0


def test_strong_url_with_conflicting_locations_requires_review(isolated_db) -> None:
    ingestor = Ingestor()
    first = _raw(
        "serpapi:anonymous-url",
        "https://www.linkedin.com/jobs/view/4464028174",
        source="serpapi",
    ).model_copy(
        update={"company": "Entreprise non communiquée", "location": "75 - Paris"}
    )
    second = _raw(
        "linkedin:anonymous-url",
        "https://www.linkedin.com/jobs/view/4464028174",
        source="linkedin",
    ).model_copy(
        update={"company": "Entreprise non communiquée", "location": "69 - Lyon"}
    )

    first_report = ingestor._persist("serpapi", [first])
    second_report = ingestor._persist("linkedin", [second])

    assert second_report.aliases_created == 0
    assert second_report.duplicate_review_ids == second_report.job_ids
    assert backfill_duplicate_reviews() == 0
    with session_scope() as session:
        alias = session.query(Job).filter(Job.external_id == second.external_id).one()
        assert alias.canonical_job_id is None
        assert alias.possible_duplicate_of_id == first_report.job_ids[0]
        assert alias.duplicate_review_status == JobDuplicateStatus.PENDING
        assert alias.duplicate_match_type == "exact_url"
        assert alias.duplicate_confidence == 1.0


def test_legacy_france_travail_id_does_not_create_another_row_or_application(isolated_db) -> None:
    native_id = "213HYVJ"
    url = f"https://candidat.francetravail.fr/offres/recherche/detail/{native_id}"
    with session_scope() as session:
        original = upsert_job(
            session,
            external_id="francetravail:old-hash",
            title="Data Scientist",
            company="Acme SAS",
            description=_DESCRIPTION,
            application_url=url,
            source="francetravail",
            source_data={"id": native_id},
        )
        application = create_or_get_application(session, original.id)
        application.status = JobStatus.SENT
        original.status = JobStatus.SENT
        original_id, application_id = original.id, application.id

    # A changed URL must not defeat the durable source ID.
    raw = _raw(
        make_external_id("francetravail", native_id),
        "https://example.test/changed-application-url",
        source="francetravail",
    ).model_copy(update={"source_data": {"id": native_id}})
    report = Ingestor()._persist("francetravail", [raw])

    assert report.persisted == 0
    assert report.aliases_created == 0
    assert report.skipped_existing_during_persist == 1
    with session_scope() as session:
        assert session.query(Job).count() == 1
        assert session.query(Application).count() == 1
        assert session.get(Job, original_id).external_id == "francetravail:old-hash"
        assert session.get(Application, application_id).job_id == original_id
        assert session.get(Application, application_id).status == JobStatus.SENT


def test_collection_skips_legacy_france_travail_id_without_consuming_new_offer(
    isolated_db,
) -> None:
    with session_scope() as session:
        upsert_job(
            session,
            external_id="francetravail:old-hash",
            title="Data Scientist",
            company="Acme SAS",
            description=_DESCRIPTION,
            source="francetravail",
            source_data={"id": "213HYVJ"},
        )
        known_index = _KnownJobIndex.from_jobs(session.query(Job).all())

    old = _raw(
        make_external_id("francetravail", "213HYVJ"),
        "https://example.test/old",
        source="francetravail",
    ).model_copy(update={"source_data": {"id": "213HYVJ"}})
    new = _raw(
        make_external_id("francetravail", "NEW123"),
        "https://example.test/new",
        source="francetravail",
    ).model_copy(update={"source_data": {"id": "NEW123"}})

    class Scraper:
        name = "francetravail"

        def search(self, query, location=None, *, max_results=None, **kwargs):  # noqa: ANN001, ARG002
            yield old
            yield new

    result = collect_round_robin(
        scraper=Scraper(),
        queries=["Data Scientist"],
        location=None,
        max_results=2,
        search_kwargs={},
        known_index=known_index,
    )

    assert result.raw_jobs == [new]
    assert result.skipped_existing == 1
    unrelated_source = old.model_copy(
        update={"external_id": "linkedin:other-offer", "source": "linkedin"}
    )
    assert not known_index.matches_source_identity(unrelated_source)


def test_same_france_travail_id_in_one_batch_creates_only_one_job(isolated_db) -> None:
    first = _raw(
        "francetravail:first-hash",
        "https://example.test/first-url",
        source="francetravail",
    ).model_copy(update={"source_data": {"id": "213HYVJ"}})
    second = first.model_copy(
        update={
            "external_id": "francetravail:second-hash",
            "application_url": "https://example.test/changed-url",
        }
    )

    report = Ingestor()._persist("francetravail", [first, second])

    assert report.inserted == 1
    assert report.skipped_existing_during_persist == 1
    assert report.aliases_created == 0
    with session_scope() as session:
        assert session.query(Job).count() == 1


def test_probable_duplicate_is_visible_but_is_not_processed_or_generated(isolated_db) -> None:
    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        application = create_or_get_application(session, duplicate_id)
        application.status = JobStatus.READY_FOR_FORM_SUBMISSION
    service = DesktopService()

    rows = service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    assert [row.id for row in rows] == [duplicate_id]
    assert rows[0].status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert rows[0].duplicate_review_status == JobDuplicateStatus.PENDING
    assert rows[0].filter_disposition == ""
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


@pytest.mark.parametrize("old_rejected,new_rejected", [(True, True), (True, False), (False, True), (False, False)])
def test_explicit_filtering_prepares_both_offers_for_review(isolated_db, old_rejected, new_rejected) -> None:
    from smartapply.pipeline import Pipeline

    root_id, duplicate_id = _create_review_pair()
    with session_scope() as session:
        for job_id, rejected in ((root_id, old_rejected), (duplicate_id, new_rejected)):
            job = session.get(Job, job_id)
            job.contract_type = "stage" if rejected else "CDI"

    Pipeline().filter_pending(job_ids=[root_id, duplicate_id])
    service = DesktopService()
    rows = service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)
    assert [row.id for row in rows] == ([] if old_rejected and new_rejected else [duplicate_id])
    with session_scope() as session:
        for job_id, rejected in ((root_id, old_rejected), (duplicate_id, new_rejected)):
            job = session.get(Job, job_id)
            assert bool(job.archived_at) == rejected
            assert job.analyzed_at is None
            assert job.score is not None
            if rejected:
                assert job.score.components["rejection_stage"] == "local_filter"
            else:
                assert job.filtered_at is not None
        duplicate = session.get(Job, duplicate_id)
        assert duplicate.duplicate_review_status == JobDuplicateStatus.PENDING
        assert duplicate_id not in {job.id for job in list_pending_processing(session)}

    assert Pipeline().filter_pending(job_ids=[root_id, duplicate_id]).total == 0
    assert Pipeline().analyze_jobs([duplicate_id]).analyzed == 0


@pytest.mark.parametrize(
    "reference_disposition,new_disposition,visible",
    [
        ("pending_relevance", "pending_relevance", True),
        ("pending_relevance", "rejected", True),
        ("rejected", "pending_relevance", True),
        ("relevant", "pending_relevance", True),
        ("pending_relevance", "relevant", True),
        ("", "pending_relevance", True),
        ("rejected", "rejected", False),
    ],
)
def test_duplicate_active_toggle_requires_at_least_one_non_rejected_offer(
    isolated_db, reference_disposition, new_disposition, visible
) -> None:
    reference_id, new_id = _create_review_pair()
    with session_scope() as session:
        for job_id, disposition in (
            (reference_id, reference_disposition),
            (new_id, new_disposition),
        ):
            session.get(Job, job_id).filtered_at = datetime.now(timezone.utc)
            set_score(session, job_id, components={"filter_disposition": disposition})

    service = DesktopService()
    assert [row.id for row in service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)] == [new_id]
    rows = service.list_jobs(
        status=JobStatus.DUPLICATE_REVIEW, only_pending_duplicates=True
    )
    assert [row.id for row in rows] == ([new_id] if visible else [])


def test_duplicate_pending_toggle_applies_limit_after_filtering(isolated_db) -> None:
    first_reference, first_new = _create_review_pair()
    second_company = "Another Company"
    first = _raw("other:reference", "https://other.test/jobs/1").model_copy(
        update={"company": second_company}
    )
    second = _raw("other:new", "https://other.test/jobs/2").model_copy(
        update={"company": second_company}
    )
    ingestor = Ingestor()
    second_reference = ingestor._persist("serpapi", [first]).job_ids[0]
    second_new = ingestor._persist("serpapi", [second]).job_ids[0]
    with session_scope() as session:
        for job_id, disposition in (
            (first_reference, "rejected"),
            (first_new, "rejected"),
            (second_reference, "pending_relevance"),
            (second_new, "pending_relevance"),
        ):
            session.get(Job, job_id).filtered_at = datetime.now(timezone.utc)
            set_score(session, job_id, components={"filter_disposition": disposition})

    rows = DesktopService().list_jobs(
        status=JobStatus.DUPLICATE_REVIEW,
        sort_key="company",
        sort_ascending=True,
        limit=1,
        only_pending_duplicates=True,
    )
    assert [row.id for row in rows] == [second_new]


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


def test_republication_preserves_sent_dossier_documents_and_source_history(isolated_db) -> None:
    from smartapply.database.repository import add_document

    root_id, new_id = _create_review_pair()
    submitted_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    with session_scope() as session:
        application = create_or_get_application(session, root_id)
        application.status = JobStatus.SENT
        application.form_submitted_at = submitted_at
        application.notes = "Entretien à suivre"
        application.cv_docx_path = "/documents/original/CV.docx"
        application.form_submission_url = "https://acme.test/jobs/1"
        application.updated_at = submitted_at
        add_document(session, application.id, doc_type="motivation_letter", content="Lettre originale")
        app_id = application.id
        new = session.get(Job, new_id)
        new.description = "Description republiée"

    service = DesktopService()
    assert service.resolve_duplicate(new_id, same_offer=True)
    with session_scope() as session:
        new = session.get(Job, new_id)
        old = session.get(Job, root_id)
        application = session.get(Application, app_id)
        assert new.description == "Description republiée"
        assert new.application_url == "https://acme.test/jobs/2"
        assert old.application_url == "https://acme.test/jobs/1"
        assert old.canonical_job_id == new_id
        assert old.status == JobStatus.ARCHIVED
        assert application.job_id == new_id
        assert application.notes == "Entretien à suivre"
        assert application.cv_docx_path == "/documents/original/CV.docx"
        assert application.form_submission_url == "https://acme.test/jobs/1"
        assert application.form_submitted_at.replace(tzinfo=timezone.utc) == submitted_at
        assert application.updated_at.replace(tzinfo=timezone.utc) == submitted_at
        assert application.documents[0].content == "Lettre originale"
        assert session.query(Application).count() == 1
    assert not service.get_job(new_id).can_send
    assert not service.get_job(new_id).can_generate
    assert not service.resolve_duplicate(new_id, same_offer=True)


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
    row = next(row for row in service.list_jobs(status=JobStatus.ARCHIVED) if row.id == active_id)
    detail = service.get_job(active_id)
    for item in (row, detail):
        assert item.id == active_id
        assert item.status == JobStatus.ARCHIVED
        assert item.application_id == app_id
        assert item.related_application_id is None
        assert item.can_generate is False
    assert service.shortlist_summary().ready_to_generate == 0
    assert service.shortlisted_generation_ids() == []
    assert service.get_application(detail.application_id).cv_docx_path.endswith("CV.docx")

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
        assert app.job_id == active_id
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


def test_confirm_duplicate_keeps_archived_submitted_history_secondary(isolated_db) -> None:
    root_id, active_id = _create_review_pair()
    submitted_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    with session_scope() as session:
        historical = create_or_get_application(session, root_id)
        historical.status = JobStatus.ARCHIVED
        historical.form_submitted_at = submitted_at
        historical.notes = "Ancien envoi conservé"
        root = session.get(Job, root_id)
        root.status = JobStatus.ARCHIVED
        root.archived_at = datetime.now(timezone.utc)

        active = create_or_get_application(session, active_id)
        active.status = JobStatus.SENT
        active.form_submitted_at = datetime(2025, 2, 3, tzinfo=timezone.utc)
        active_job = session.get(Job, active_id)
        active_job.status = JobStatus.SENT
        historical_id, active_application_id = historical.id, active.id

    assert DesktopService().resolve_duplicate(active_id, same_offer=True)
    with session_scope() as session:
        historical = session.get(Application, historical_id)
        active = session.get(Application, active_application_id)
        assert historical.job_id == root_id
        assert historical.status == JobStatus.ARCHIVED
        assert historical.form_submitted_at is not None
        assert historical.notes == "Ancien envoi conservé"
        assert active.job_id == active_id
        assert application_for_duplicate_group(session, active_id).id == active_application_id


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
        assert alias.canonical_job_id is None
        assert alias.status == application_status
        assert canonical_job(session, root_id).id == duplicate_id
        assert alias.duplicate_review_status == JobDuplicateStatus.CONFIRMED
        grouped_application = application_for_duplicate_group(session, duplicate_id)
        assert grouped_application is not None
        assert grouped_application.status == application_status
        assert grouped_application.job_id == duplicate_id

    service = DesktopService()
    detail = service.get_job(duplicate_id)
    assert detail.status == application_status
    assert detail.application_id == application.id
    assert detail.can_generate is False
    assert duplicate_id in {row.id for row in service.list_jobs(status=application_status)}
    if application_status == JobStatus.SENT:
        assert detail.can_send is False

    report = ApplyReport(job_id=duplicate_id, application_id=None)
    with pytest.raises(ApplicationAlreadyExistsError):
        ApplicationPersistenceMixin()._reserve_application_id(report)

    with session_scope() as session:
        assert session.query(Application).count() == 1
