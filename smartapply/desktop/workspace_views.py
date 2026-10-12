"""Bounded desktop read models, independent of execution and admission policy.

History pages contain metadata. Only the selected journal loads item details.
Queue membership remains global and uses the same admission proof as execution.
"""

from collections import Counter, defaultdict
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from smartapply.database import session_scope
from smartapply.database.analysis_batches import (
    ACTIVE_ANALYSIS_RUN_STATES,
    AnalysisBatchItem,
    AnalysisBatchRun,
)
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.generation_runs import (
    RESERVED_GENERATION_STATES,
    TERMINAL_GENERATION_ITEM_STATES,
    GenerationItem,
    GenerationRun,
)
from smartapply.database.models import Job
from smartapply.jobsearch.status import display_status
from smartapply.pipeline.batch_analysis.presentation import _date, _experience, analysis_workspace
from smartapply.pipeline.process.analysis_admission import (
    analysis_queue_job_ids,
    reserved_analysis_job_ids,
)

HISTORY_PAGE_SIZE = 20
CANDIDATE_PAGE_SIZE = 100


def _page(value, total, size):
    pages = max(1, ceil(total / size))
    return min(max(1, int(value)), pages), pages


def _history(session, kind, view):
    generation = kind == "generation"
    run_cls, item_cls = (GenerationRun, GenerationItem) if generation else (AnalysisBatchRun, AnalysisBatchItem)
    visible = [] if generation else [run_cls.state != "cancelled"]
    total = session.scalar(select(func.count()).select_from(run_cls).where(*visible)) or 0
    page, pages = _page(view.get("history_page", 1), total, HISTORY_PAGE_SIZE)
    numbered = select(
        run_cls.id, run_cls.state, run_cls.cancel_requested, run_cls.created_at,
        *([run_cls.mode] if generation else []),
        func.row_number().over(order_by=(run_cls.created_at, run_cls.id)).label("sequence"),
    ).subquery()
    query = select(numbered).order_by(numbered.c.created_at.desc(), numbered.c.id.desc())
    if not generation:
        query = query.where(numbered.c.state != "cancelled")
    rows = session.execute(query.limit(HISTORY_PAGE_SIZE).offset((page - 1) * HISTORY_PAGE_SIZE)).all()
    active_states = ("active", "attention", "blocked") if generation else ACTIVE_ANALYSIS_RUN_STATES
    active_rows = session.execute(select(numbered).where(numbered.c.state.in_(active_states))
                                  .order_by(numbered.c.created_at.desc(), numbered.c.id.desc())).all()
    active_id = active_rows[0].id if active_rows else ""
    selected_id = str(view.get("selected_run_id") or "")
    if selected_id:
        selected_row = session.execute(select(numbered).where(numbered.c.id == selected_id)).first()
        if selected_row is None or (not generation and selected_row.state == "cancelled"):
            selected_id = ""
    if not selected_id:
        selected_id = (active_id if page == 1 else "") or (rows[0].id if rows else "")
    # Include formerly active runs for one final observation, even on an old
    # history page. Browsing history must not stop monitoring another run.
    monitor_ids = set(view.get("monitor_run_ids", ())) | {row.id for row in active_rows}
    ids = {row.id for row in rows} | monitor_ids | ({selected_id} if selected_id else set())
    metadata = session.execute(select(numbered).where(numbered.c.id.in_(ids))
                               .order_by(numbered.c.created_at.desc(), numbered.c.id.desc())).all()
    counts = defaultdict(Counter)
    for run_id, state, count in session.execute(select(item_cls.run_id, item_cls.state, func.count())
                                              .where(item_cls.run_id.in_(ids))
                                              .group_by(item_cls.run_id, item_cls.state)):
        counts[run_id][state] = count
    summaries = {}
    for row in metadata:
        c = counts[row.id]
        failed = c["failed"] if generation else sum(n for state, n in c.items() if state.endswith("_failed"))
        completed = c["completed"]
        status = "cancelling" if row.cancel_requested and row.state in {"active", "attention"} else row.state
        summaries[row.id] = {
            "id": row.id, "sequence": row.sequence, "kind": kind, "status": status,
            **({"mode": row.mode} if generation else {}),
            "status_label": {"active": "En cours", "attention": "Envoi à vérifier", "blocked": "À reprendre",
                             "completed": "Terminé", "partial": "À reprendre", "failed": "Échec",
                             "paused": "Arrêté · reprise possible", "cancelled": "Arrêté",
                             "cancelling": "Arrêt en cours"}.get(status, status),
            "is_active": row.state in active_states if generation else row.state in {"active", "attention"},
            "can_cancel": row.state in active_states and not row.cancel_requested,
            "total": sum(c.values()), "analyzed": completed, "generated": completed,
            "failed": failed, "screened_out": c["filtered_after_screening"],
            "skipped": c["stale"] + c["transferred"] if generation else c["skipped"],
            "created_at": _date(row.created_at),
        }
    result = {
        "desktop_view": True, "view_revision": view.get("view_revision", 0),
        "history_page": page, "history_page_size": HISTORY_PAGE_SIZE,
        "history_total_pages": pages, "history_total_count": total,
        "runs": [summaries[row.id] for row in rows],
        "monitor_runs": [summaries[row.id] for row in metadata if row.id in monitor_ids],
        "active_run_id": active_id, "active_run_count": len(active_rows),
        "polling_run_count": sum(summary["is_active"] for summary in summaries.values()),
    }
    return result, selected_id


def _candidates(session, view):
    ids = analysis_queue_job_ids(session)
    checkpoints = set(session.scalars(select(AnalysisCheckpoint.job_id).where(AnalysisCheckpoint.job_id.in_(ids))))
    jobs = session.scalars(select(Job).where(Job.id.in_(ids)).options(load_only(
        Job.id, Job.title, Job.source_company, Job.location, Job.source,
        Job.display_metadata, Job.status, Job.published_date, raiseload=True,
    )).order_by(Job.published_date.desc(), Job.id.desc()))
    candidates = [{
        "id": job.id, "title": job.title, "company": job.source_company,
        "location": job.location or "", "experience": _experience(job), "source": job.source,
        "offer_status": display_status(job.status), "published_date": _date(job.published_date),
        "state": "evaluation_pending" if job.id in checkpoints else "extraction_pending",
        "state_label": "À évaluer" if job.id in checkpoints else "À extraire",
    } for job in jobs]
    query = str(view.get("candidate_query", "")).strip().lower()
    matching = [row for row in candidates if not query or query in " ".join(
        str(row.get(key, "")) for key in ("title", "company", "location", "id", "state_label")
    ).lower()]
    key = view.get("candidate_sort", "published_date")
    if key not in {"id", "title", "company", "location", "experience", "source", "state_label", "published_date"}:
        key = "published_date"
    def sort_value(row):
        value = row[key]
        if key == "experience":
            try:
                value = float(value.split("+")[0]) if value != "—" else -1
            except (ValueError, TypeError):
                value = -1
        return value.lower() if isinstance(value, str) else value
    matching.sort(key=lambda row: row["id"])
    matching.sort(key=sort_value, reverse=not bool(view.get("candidate_ascending", False)))
    page, pages = _page(view.get("candidate_page", 1), len(matching), CANDIDATE_PAGE_SIZE)
    return {
        "candidates": matching[(page - 1) * CANDIDATE_PAGE_SIZE:page * CANDIDATE_PAGE_SIZE],
        "candidate_ids": [row["id"] for row in candidates],
        "matching_candidate_ids": [row["id"] for row in matching],
        "candidates_count": len(candidates), "pending_count": len(candidates),
        "candidate_count": len(matching), "candidate_page": page,
        "candidate_page_size": CANDIDATE_PAGE_SIZE, "candidate_total_pages": pages,
        "candidate_revision": view.get("candidate_revision", 0),
    }


def analysis_workspace_view(**view):
    with session_scope() as session:
        result, selected_id = _history(session, "analysis", view)
        result["reserved_job_ids"] = sorted(reserved_analysis_job_ids(session))
        if view.get("include_candidates", True):
            result.update(_candidates(session, view))
    details = analysis_workspace(run_ids=[selected_id], include_candidates=False)["runs"] if selected_id else []
    result["selected_run"] = details[0] if details else {}
    return result


def generation_workspace_view(**view):
    from smartapply.pipeline.generation.offer_resume import resumable_generation_job_ids
    from smartapply.pipeline.generation.presentation import generation_workspace

    with session_scope() as session:
        result, selected_id = _history(session, "generation", view)
        result["reserved_job_ids"] = sorted(set(session.scalars(select(GenerationItem.job_id).join(GenerationRun).where(
            GenerationRun.state.in_(RESERVED_GENERATION_STATES),
            GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
        ))))
        result["resumable_job_ids"] = sorted(resumable_generation_job_ids(session))
    details = generation_workspace(run_ids=[selected_id], include_availability=False)["runs"] if selected_id else []
    result["selected_run"] = details[0] if details else {}
    return result
