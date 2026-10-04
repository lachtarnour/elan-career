"""Duplicate-review and canonical-offer repository helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from smartapply.database.models import Application, Job, JobDuplicateStatus, JobStatus


def canonical_job(session: Session, job_id: int) -> Job | None:
    """Resolve a confirmed alias to its canonical offer row."""
    current = session.get(Job, job_id)
    if current is None:
        return None
    seen: set[int] = set()
    while current.canonical_job_id is not None:
        if int(current.id) in seen:
            break
        seen.add(int(current.id))
        next_job = session.get(Job, int(current.canonical_job_id))
        if next_job is None:
            break
        current = next_job
    return current


def duplicate_group_ids(session: Session, job_id: int) -> set[int]:
    """Return the canonical row, its aliases and a pending review row."""
    job = session.get(Job, job_id)
    if job is None:
        return set()

    root = canonical_job(session, job_id)
    root_id = int(root.id if root is not None else job.id)
    ids = set(
        int(value)
        for value in session.scalars(
            select(Job.id).where(
                or_(Job.id == root_id, Job.canonical_job_id == root_id)
            )
        )
    )
    root_ids = {root_id}
    if job.duplicate_review_status == JobDuplicateStatus.PENDING:
        ids.add(int(job.id))
        if job.possible_duplicate_of_id is not None:
            possible_root = canonical_job(session, int(job.possible_duplicate_of_id))
            if possible_root is not None:
                root_ids.add(int(possible_root.id))
                ids.update(
                    int(value)
                    for value in session.scalars(
                        select(Job.id).where(
                            or_(
                                Job.id == int(possible_root.id),
                                Job.canonical_job_id == int(possible_root.id),
                            )
                        )
                    )
                )

    # A root must also be protected while another review row points to it. In
    # particular, this catches an already archived application attached to a
    # later source row before the user has confirmed the alias.
    for other in session.scalars(select(Job)).all():
        if other.duplicate_review_status != JobDuplicateStatus.PENDING:
            continue
        reference_root = _pending_reference_root(session, other)
        if reference_root is not None and int(reference_root.id) in root_ids:
            ids.add(int(other.id))
    return ids


def _pending_reference_root(session: Session, job: Job) -> Job | None:
    """Follow pending references and confirmed aliases without looping."""
    current = job
    seen: set[int] = set()
    while int(current.id) not in seen:
        seen.add(int(current.id))
        if (
            current.duplicate_review_status == JobDuplicateStatus.PENDING
            and current.possible_duplicate_of_id is not None
        ):
            next_job = session.get(Job, int(current.possible_duplicate_of_id))
            if next_job is not None:
                current = next_job
                continue
        return canonical_job(session, int(current.id))
    return None


def _application_priority():
    """Prefer an active workflow, then preserved submission history."""
    return (
        case(
            (
                Application.status.in_(
                    [JobStatus.SENT, JobStatus.INTERVIEW, JobStatus.REJECTED]
                ),
                0,
            ),
            (Application.form_submitted_at.is_not(None), 1),
            (Application.status != JobStatus.ARCHIVED, 2),
            else_=3,
        ),
        Application.updated_at.desc(),
        Application.id.desc(),
    )


def application_for_duplicate_group(session: Session, job_id: int) -> Application | None:
    """Return the priority dossier, preserving evidence of an existing submission."""
    ids = duplicate_group_ids(session, job_id)
    if not ids:
        return None
    return session.execute(
        select(Application)
        .where(Application.job_id.in_(ids))
        .order_by(*_application_priority())
        .limit(1)
    ).scalar_one_or_none()


def application_ids_for_confirmed_groups(session: Session, job_ids: list[int]) -> dict[int, int]:
    """Find existing dossiers for a batch without treating pending matches as confirmed."""
    if not job_ids:
        return {}
    parents = dict(
        session.execute(
            select(Job.id, Job.canonical_job_id).where(Job.canonical_job_id.is_not(None))
        ).all()
    )

    def root_id(job_id: int) -> int:
        seen: set[int] = set()
        while job_id in parents and job_id not in seen:
            seen.add(job_id)
            job_id = parents[job_id]
        return job_id

    applications_by_root: dict[int, int] = {}
    for application_id, owner_id in session.execute(
        select(Application.id, Application.job_id).order_by(
            *_application_priority()
        )
    ):
        applications_by_root.setdefault(root_id(owner_id), application_id)
    return {
        job_id: applications_by_root[root_id(job_id)]
        for job_id in job_ids
        if root_id(job_id) in applications_by_root
    }


def pending_duplicate_job(session: Session, job_id: int) -> Job | None:
    """Return the pending review row for an offer, if one exists."""
    job = session.get(Job, job_id)
    if job is None:
        return None
    if job.duplicate_review_status == JobDuplicateStatus.PENDING:
        return job
    return None


def pending_duplicate_for_group(session: Session, job_id: int) -> Job | None:
    """Return an unresolved review on the canonical row or a related offer."""
    root = canonical_job(session, job_id)
    if root is None:
        return None
    if root.duplicate_review_status == JobDuplicateStatus.PENDING:
        return root
    for job in session.scalars(
        select(Job).where(Job.duplicate_review_status == JobDuplicateStatus.PENDING)
    ):
        if int(job.id) == int(job_id):
            continue
        reference_root = _pending_reference_root(session, job)
        if reference_root is not None and int(reference_root.id) == int(root.id):
            return job
    return None


def confirm_duplicate(session: Session, job_id: int) -> Job | None:
    """Keep the active republication and carry forward its application history."""
    job = pending_duplicate_job(session, job_id)
    if job is None or job.possible_duplicate_of_id is None:
        return None
    root = canonical_job(session, int(job.possible_duplicate_of_id))
    if root is None or int(root.id) == int(job.id):
        return None

    # Only this decision and previously confirmed aliases may contribute a
    # dossier. The broader duplicate_group_ids also includes unconfirmed
    # siblings and is deliberately reserved for conservative generation guards.
    # The reference may itself still await a decision against a third offer.
    # Confirming job–root resolves only that edge, not root's upstream review.
    upstream_review = (
        (root.possible_duplicate_of_id, root.duplicate_match_type, root.duplicate_confidence)
        if root.duplicate_review_status == JobDuplicateStatus.PENDING
        and root.possible_duplicate_of_id is not None
        else None
    )
    members = [root, job]
    group_roots = {int(root.id), int(job.id)}
    for alias in session.scalars(select(Job).where(Job.canonical_job_id.is_not(None))):
        if alias.id not in group_roots and canonical_job(session, int(alias.id)).id in group_roots:
            members.append(alias)
    applications = session.scalars(
        select(Application).where(Application.job_id.in_([member.id for member in members]))
        .order_by(*_application_priority())
    ).all()
    # Archived dossiers are retained on their source aliases, including their
    # submission evidence. Only two live workflows require a manual choice.
    if any(application.status != JobStatus.ARCHIVED for application in applications[1:]):
        raise ValueError(
            "Ces offres possèdent plusieurs dossiers. Le regroupement nécessite "
            "de choisir le dossier à conserver. Aucun dossier n’a été déplacé."
        )

    def priority(offer: Job) -> tuple[bool, float, float, int]:
        def timestamp(value: datetime | None) -> float:
            if value is None:
                return float("-inf")
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()

        return (
            not bool(offer.archived_at or offer.status == JobStatus.ARCHIVED),
            timestamp(offer.published_date or offer.scraped_at),
            timestamp(offer.scraped_at),
            int(offer.id),
        )

    winner = max((root, job), key=priority)
    if applications:
        application = applications[0]
        if winner.application is not None and winner.application.id != application.id:
            raise ValueError(
                "La publication principale possède déjà un autre dossier. "
                "Les deux dossiers ont été conservés ; leur rattachement nécessite un choix."
            )
        if application.job_id != winner.id:
            updated_at = application.updated_at
            application.job = winner
            application.updated_at = updated_at
            flag_modified(application, "updated_at")
        winner.status = application.status
        if application.status == JobStatus.ARCHIVED:
            winner.archived_at = winner.archived_at or datetime.now(timezone.utc)

    for member in members:
        member.canonical_job_id = None if member.id == winner.id else int(winner.id)
        member.possible_duplicate_of_id = None
        member.duplicate_review_status = JobDuplicateStatus.CONFIRMED
        if member.id != winner.id:
            member.archived_at = member.archived_at or datetime.now(timezone.utc)
            member.shortlisted_at = None
            member.shortlist_origin = None
            member.status = JobStatus.ARCHIVED
    if upstream_review is not None:
        reference_id, match_type, confidence = upstream_review
        reference = canonical_job(session, int(reference_id))
        if reference is not None and reference.id != winner.id:
            winner.possible_duplicate_of_id = int(reference.id)
            winner.duplicate_review_status = JobDuplicateStatus.PENDING
            winner.duplicate_match_type = match_type
            winner.duplicate_confidence = confidence
    return job


def reject_duplicate(session: Session, job_id: int) -> Job | None:
    """Reject a review match and release the row as an independent offer."""
    job = pending_duplicate_job(session, job_id)
    if job is None:
        return None
    was_archived = bool(job.archived_at or job.status == JobStatus.ARCHIVED)
    job.possible_duplicate_of_id = None
    job.duplicate_review_status = JobDuplicateStatus.REJECTED
    if was_archived:
        # A rejected match must not resurrect an offer that was already
        # archived before it entered the duplicate-review queue.
        job.status = JobStatus.ARCHIVED
        return job
    job.archived_at = None
    job.shortlisted_at = None
    job.shortlist_origin = None
    job.status = (
        JobStatus.ANALYZED
        if job.analyzed_at is not None
        else JobStatus.FILTERED
        if job.filtered_at is not None
        else JobStatus.SCRAPED
    )
    return job
