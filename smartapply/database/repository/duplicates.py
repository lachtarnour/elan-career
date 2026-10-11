"""Duplicate-review and canonical-offer repository helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from smartapply.database.models import Application, Job, JobDuplicateStatus, JobScore, JobStatus
from smartapply.jobsearch.archive_reasons import (
    ARCHIVE_ORIGINS,
    archive_origin_from_components,
    archive_rejection_reasons,
)


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
    relations = _related_offer_rows(session, [job_id], include_pending=True)
    job = relations.get(job_id)
    if job is None:
        return set()
    root_id = _canonical_relation_id(relations, job_id)
    ids = _direct_alias_ids(relations, root_id)
    root_ids = {root_id}
    if job.duplicate_review_status == JobDuplicateStatus.PENDING:
        ids.add(job.id)
        if job.possible_duplicate_of_id is not None:
            possible_root_id = _canonical_relation_id(relations, job.possible_duplicate_of_id)
            if possible_root_id is not None:
                root_ids.add(possible_root_id)
                ids.update(_direct_alias_ids(relations, possible_root_id))

    # A root must also be protected while another review row points to it. In
    # particular, this catches an already archived application attached to a
    # later source row before the user has confirmed the alias.
    for other in relations.values():
        if other.duplicate_review_status != JobDuplicateStatus.PENDING:
            continue
        if _pending_relation_root_id(relations, other.id) in root_ids:
            ids.add(other.id)
    return ids


class _OfferRelations(NamedTuple):
    id: int
    canonical_job_id: int | None
    possible_duplicate_of_id: int | None
    duplicate_review_status: str | None


def _related_offer_rows(
    session: Session, job_ids: list[int], *, include_pending: bool = False,
) -> dict[int, _OfferRelations]:
    """Read only identity columns in the requested connected components.

    Each recursive branch uses the indexed relation columns. UNION, rather
    than UNION ALL, also terminates on legacy cycles. Missing target IDs stay
    in the traversal, so aliases sharing a missing parent remain connected.
    """
    related = select(Job.id).where(or_(
        Job.id.in_(job_ids), Job.canonical_job_id.in_(job_ids),
    )).cte("related_offer_ids", recursive=True)
    branches = [
        select(Job.canonical_job_id).join(related, Job.id == related.c.id).where(
            Job.canonical_job_id.is_not(None),
        ),
        select(Job.id).join(related, Job.canonical_job_id == related.c.id),
    ]
    if include_pending:
        branches.extend([
            select(Job.possible_duplicate_of_id).join(related, Job.id == related.c.id).where(
                Job.duplicate_review_status == JobDuplicateStatus.PENDING,
                Job.possible_duplicate_of_id.is_not(None),
            ),
            select(Job.id).join(related, Job.possible_duplicate_of_id == related.c.id).where(
                Job.duplicate_review_status == JobDuplicateStatus.PENDING,
            ),
        ])
    related = related.union(*branches)
    return {
        row.id: _OfferRelations(*row)
        for row in session.execute(select(
            Job.id, Job.canonical_job_id, Job.possible_duplicate_of_id,
            Job.duplicate_review_status,
        ).where(Job.id.in_(select(related.c.id))))
    }


def _canonical_relation_id(relations: dict[int, _OfferRelations], job_id: int) -> int | None:
    """Match canonical_job, including cycles and absent target rows."""
    current = relations.get(job_id)
    if current is None:
        return None
    seen: set[int] = set()
    while current.canonical_job_id is not None and current.id not in seen:
        seen.add(current.id)
        next_row = relations.get(current.canonical_job_id)
        if next_row is None:
            break
        current = next_row
    return current.id


def _direct_alias_ids(relations: dict[int, _OfferRelations], root_id: int | None) -> set[int]:
    if root_id is None:
        return set()
    return {row.id for row in relations.values()
            if row.id == root_id or row.canonical_job_id == root_id}


def _pending_relation_root_id(relations: dict[int, _OfferRelations], job_id: int) -> int | None:
    current = relations[job_id]
    seen: set[int] = set()
    while current.id not in seen:
        seen.add(current.id)
        if (current.duplicate_review_status == JobDuplicateStatus.PENDING
                and current.possible_duplicate_of_id is not None):
            next_row = relations.get(current.possible_duplicate_of_id)
            if next_row is not None:
                current = next_row
                continue
        return _canonical_relation_id(relations, current.id)
    return None


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
                    [JobStatus.SENT, JobStatus.INTERVIEW]
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
    relations = _related_offer_rows(session, job_ids)
    parents = {row.id: row.canonical_job_id for row in relations.values()
               if row.canonical_job_id is not None}

    def root_id(job_id: int) -> int:
        seen: set[int] = set()
        while job_id in parents and job_id not in seen:
            seen.add(job_id)
            job_id = parents[job_id]
        return job_id

    applications_by_root: dict[int, int] = {}
    for application_id, owner_id in session.execute(
        select(Application.id, Application.job_id).where(
            Application.job_id.in_(set(relations) | set(job_ids)
                                   | {root_id(job_id) for job_id in job_ids}),
        ).order_by(
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
    relations = _related_offer_rows(session, [job_id], include_pending=True)
    root_id = _canonical_relation_id(relations, job_id)
    if root_id is None:
        return None
    if relations[root_id].duplicate_review_status == JobDuplicateStatus.PENDING:
        return session.get(Job, root_id)
    for other_id in sorted(relations):
        if other_id == job_id:
            continue
        other = relations[other_id]
        if (other.duplicate_review_status == JobDuplicateStatus.PENDING
                and _pending_relation_root_id(relations, other_id) == root_id):
            return session.get(Job, other_id)
    return None


def _clear_restored_review_route(session: Session, resolved_job_id: int) -> None:
    """Remove the resolved review pointer while preserving the restoration audit."""
    for score in session.scalars(select(JobScore).where(
        func.json_extract(
            JobScore.components, "$.manual_rescue.duplicate_review_job_id"
        ) == int(resolved_job_id)
    )):
        components = score.components
        rescue = components.get("manual_rescue") if isinstance(components, dict) else None
        if (
            not isinstance(rescue, dict)
            or type(rescue.get("duplicate_review_job_id")) is not int
            or rescue["duplicate_review_job_id"] != int(resolved_job_id)
        ):
            continue
        score.components = {
            **components,
            "manual_rescue": {
                key: value for key, value in rescue.items() if key != "duplicate_review_job_id"
            },
        }


def _archived_application_origin(source: Job, application: Application) -> dict[str, object]:
    """Keep the original archive evidence when a dossier changes offer owners."""
    components = source.score.components if source.score is not None else None
    components = components if isinstance(components, dict) else {}
    inherited = components.get("archive_inheritance")
    if _valid_archive_inheritance(inherited, application_id=int(application.id)):
        return dict(inherited)

    stage = components.get("rejection_stage")
    provenance: dict[str, object] = {
        "schema_version": 1,
        "source_job_id": int(source.id),
        "source_title": source.title,
        "source_company": source.source_company,
        "source_archived_at": source.archived_at.isoformat() if source.archived_at else None,
        "application_id": int(application.id),
        "rejection_stage": stage if isinstance(stage, str) else None,
        "rejection_reasons": archive_rejection_reasons(components),
    }
    origin = archive_origin_from_components(components)
    if origin is not None:
        provenance["origin"] = origin
    return provenance


def _valid_archive_inheritance(value: object, *, application_id: int) -> bool:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        return False
    if value["schema_version"] != 1 or value.get("application_id") != application_id:
        return False
    if type(value.get("application_id")) is not int:
        return False
    if type(value.get("source_job_id")) is not int or value["source_job_id"] <= 0:
        return False
    if not all(isinstance(value.get(field), str) for field in ("source_title", "source_company")):
        return False
    if "source_archived_at" not in value or "rejection_stage" not in value:
        return False
    archived_at = value["source_archived_at"]
    if archived_at is not None:
        if not isinstance(archived_at, str):
            return False
        try:
            datetime.fromisoformat(archived_at)
        except ValueError:
            return False
    if value["rejection_stage"] is not None and not isinstance(value["rejection_stage"], str):
        return False
    reasons = value.get("rejection_reasons")
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        return False
    origin = value.get("origin")
    return origin is None or isinstance(origin, str) and origin in ARCHIVE_ORIGINS


def _inherit_archived_application(session: Session, winner: Job, application: Application) -> None:
    """Persist provenance separately from the winning offer's local filter."""
    provenance = _archived_application_origin(application.job, application)
    components = winner.score.components if winner.score is not None else None
    components = dict(components) if isinstance(components, dict) else {}
    components["archive_inheritance"] = provenance
    origin = provenance.get("origin")
    if origin in ARCHIVE_ORIGINS:
        components["archive_origin"] = origin
    else:
        components.pop("archive_origin", None)
    if winner.score is None:
        winner.score = JobScore(components=components)
        session.add(winner.score)
    else:
        winner.score.components = components


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
            if application.status == JobStatus.ARCHIVED:
                _inherit_archived_application(session, winner, application)
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
    _clear_restored_review_route(session, int(job.id))
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
        _clear_restored_review_route(session, int(job.id))
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
    _clear_restored_review_route(session, int(job.id))
    return job
