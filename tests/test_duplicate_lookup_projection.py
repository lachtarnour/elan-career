"""Scoped duplicate lookups retain legacy graph and dossier priority semantics."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, insert, or_, select

from smartapply.database import Application, Job, JobDuplicateStatus, JobStatus, session_scope
from smartapply.database.repository.duplicates import (
    _application_priority,
    _pending_reference_root,
    _related_offer_rows,
    application_ids_for_confirmed_groups,
    canonical_job,
    duplicate_group_ids,
    pending_duplicate_for_group,
)
from smartapply.database.session import get_engine


def _legacy_group_ids(session, job_id):
    job = session.get(Job, job_id)
    if job is None:
        return set()
    root = canonical_job(session, job_id)
    root_ids = {root.id if root is not None else job.id}
    ids = set(session.scalars(select(Job.id).where(or_(
        Job.id.in_(root_ids), Job.canonical_job_id.in_(root_ids),
    ))))
    if job.duplicate_review_status == JobDuplicateStatus.PENDING:
        ids.add(job.id)
        if job.possible_duplicate_of_id is not None:
            possible_root = canonical_job(session, job.possible_duplicate_of_id)
            if possible_root is not None:
                root_ids.add(possible_root.id)
                ids.update(session.scalars(select(Job.id).where(or_(
                    Job.id == possible_root.id, Job.canonical_job_id == possible_root.id,
                ))))
    for other in session.scalars(select(Job)):
        if other.duplicate_review_status == JobDuplicateStatus.PENDING:
            root = _pending_reference_root(session, other)
            if root is not None and root.id in root_ids:
                ids.add(other.id)
    return ids


def _legacy_application_ids(session, job_ids):
    parents = dict(session.execute(select(Job.id, Job.canonical_job_id).where(
        Job.canonical_job_id.is_not(None),
    )).all())

    def root_id(job_id):
        seen = set()
        while job_id in parents and job_id not in seen:
            seen.add(job_id)
            job_id = parents[job_id]
        return job_id

    by_root = {}
    for app_id, owner_id in session.execute(select(Application.id, Application.job_id).order_by(
        *_application_priority(),
    )):
        by_root.setdefault(root_id(owner_id), app_id)
    return {job_id: by_root[root_id(job_id)] for job_id in job_ids if root_id(job_id) in by_root}


def _legacy_pending_id(session, job_id):
    root = canonical_job(session, job_id)
    if root is None:
        return None
    if root.duplicate_review_status == JobDuplicateStatus.PENDING:
        return root.id
    for job in session.scalars(select(Job).where(
        Job.duplicate_review_status == JobDuplicateStatus.PENDING,
    )):
        if job.id == job_id:
            continue
        reference = _pending_reference_root(session, job)
        if reference is not None and reference.id == root.id:
            return job.id
    return None


def _seed_graph():
    now = datetime.now(timezone.utc)
    # The missing references are intentional legacy corruption. Everything is
    # isolated in the fixture database; normal connections still enforce FKs.
    with get_engine().connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(insert(Job), [dict(
            id=job_id, external_id=f"graph:{job_id}", title="Engineer", source="manual",
            source_company="Acme", description="Description", scraped_at=now,
        ) for job_id in range(1, 19)])
        links = {
            2: (1, None, None), 3: (2, None, None),
            4: (None, 3, "pending"), 5: (None, 4, "pending"),
            6: (7, 5, "pending"), 8: (9, None, None), 9: (8, None, None),
            10: (8, None, None), 11: (None, 9, "pending"),
            12: (None, 13, "pending"), 13: (1, 12, "pending"),
            14: (900, None, None), 15: (900, None, None),
            16: (14, 901, "pending"), 17: (None, 1, "none"),
            18: (None, 902, "pending"),
        }
        for job_id, (canonical, possible, state) in links.items():
            connection.execute(Job.__table__.update().where(Job.id == job_id).values(
                canonical_job_id=canonical, possible_duplicate_of_id=possible,
                duplicate_review_status=state,
            ))
        connection.execute(insert(Application), [dict(
            id=owner, job_id=owner, status=state,
            created_at=now, updated_at=now + timedelta(days=owner),
            form_submitted_at=now if owner == 3 else None,
        ) for owner, state in [
            (1, JobStatus.ARCHIVED), (2, JobStatus.SENT),
            (3, JobStatus.READY_FOR_FORM_SUBMISSION), (4, JobStatus.ANALYZED),
            (8, JobStatus.SENT), (9, JobStatus.INTERVIEW), (10, JobStatus.ANALYZED),
            (14, JobStatus.ARCHIVED), (15, JobStatus.SENT), (900, JobStatus.ANALYZED),
        ]])
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def test_scoped_duplicate_groups_match_legacy_chains_reviews_cycles_and_missing_links(isolated_db):
    _seed_graph()
    for job_id in [*range(1, 19), 900, 999]:
        with session_scope() as session:
            expected = _legacy_group_ids(session, job_id)
        with session_scope() as session:
            assert duplicate_group_ids(session, job_id) == expected
    # Keep the existing distinction: confirmed group IDs include direct aliases,
    # while dossier lookup follows transitive canonical links.
    with session_scope() as session:
        assert duplicate_group_ids(session, 1) == {1, 2, 4, 5, 6, 13}


def test_scoped_dossier_lookup_matches_legacy_priority_for_every_graph_entry(isolated_db):
    _seed_graph()
    subsets = [[job_id] for job_id in [*range(1, 19), 900, 999]]
    subsets.extend([list(range(1, 19)), [2, 2, 3, 9, 900], []])
    for ids in subsets:
        with session_scope() as session:
            expected = _legacy_application_ids(session, ids)
        with session_scope() as session:
            assert application_ids_for_confirmed_groups(session, ids) == expected
    with session_scope() as session:
        assert application_ids_for_confirmed_groups(session, [1, 2, 3]) == {1: 2, 2: 2, 3: 2}
        assert application_ids_for_confirmed_groups(session, [14, 15, 900]) == {
            14: 15, 15: 15, 900: 15,
        }


def test_scoped_pending_lookup_matches_legacy_and_loads_only_the_selected_review(isolated_db):
    _seed_graph()
    for job_id in [*range(1, 19), 900, 999]:
        with session_scope() as session:
            expected = _legacy_pending_id(session, job_id)
        with session_scope() as session:
            actual = pending_duplicate_for_group(session, job_id)
            assert (actual.id if actual is not None else None) == expected
            assert len(session.identity_map) == (1 if expected is not None else 0)


def test_duplicate_projection_ignores_large_unrelated_payloads_with_bounded_reads(isolated_db):
    _seed_graph()
    now = datetime.now(timezone.utc)
    with get_engine().begin() as connection:
        connection.execute(insert(Job), [dict(
            id=job_id, external_id=f"unrelated:{job_id}", title="Other", source="manual",
            source_company="Other", description="large body " * 4000,
            cleaned_description="large clean body " * 4000,
            source_data={"large": "payload " * 4000}, scraped_at=now,
            duplicate_review_status=JobDuplicateStatus.PENDING,
        ) for job_id in range(1000, 2000)])
        connection.execute(insert(Application), [dict(
            job_id=job_id, status=JobStatus.SENT, created_at=now, updated_at=now,
            cv_json={"large": "CV " * 4000}, notes="notes " * 4000,
        ) for job_id in range(1000, 2000)])
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(get_engine(), "before_cursor_execute", record)
    try:
        with session_scope() as session:
            rows = _related_offer_rows(session, [8], include_pending=True)
            assert set(rows) == {8, 9, 10, 11}
            assert not session.identity_map
            assert duplicate_group_ids(session, 8) == {8, 9, 10}
            assert application_ids_for_confirmed_groups(session, [8]) == {8: 8}
            assert not session.identity_map
    finally:
        event.remove(get_engine(), "before_cursor_execute", record)
    assert len(statements) == 4
    assert all("jobs.description" not in statement for statement in statements)
    assert all("jobs.source_data" not in statement for statement in statements)
    assert all("applications.cv_json" not in statement for statement in statements)
    assert all("applications.notes" not in statement for statement in statements)
