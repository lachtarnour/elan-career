"""Small journal summaries for the desktop; prompts and profile stay unloaded."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.generation_runs import (
    RESERVED_GENERATION_STATES,
    TERMINAL_GENERATION_ITEM_STATES,
    GenerationCall,
    GenerationItem,
    GenerationItemContext,
    GenerationRun,
    GenerationWave,
)

STATUS_LABELS = {
    "active": "En cours", "attention": "Envoi à vérifier", "blocked": "À reprendre",
    "paused": "Arrêté · reprise possible", "completed": "Terminé",
    "partial": "À reprendre", "failed": "Échec", "cancelled": "Arrêté",
}
PHASES = (
    ("cv_adaptation", "Sélection du CV"),
    ("cv_selection_correction", "Correction de la sélection"),
    ("assembly", "Assemblage et validation Python"),
    ("application_texts", "Résumé et lettre"),
    ("document_review", "Relecture"),
    ("document_correction", "Correction"),
    ("confirmation", "Confirmation"),
    ("motivation_letter_repair", "Correction de la lettre"),
    ("rendering", "Validation et rendu Python"),
)
OPTIONAL = {"cv_selection_correction", "document_correction", "confirmation", "motivation_letter_repair"}


def _date(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def generation_workspace(*, run_ids=None, include_availability=True):
    """Summarize all runs, or hydrate only selected journals for a detail view.

    Availability is optional because detail hydration does not decide whether
    another offer can start or resume generation. Run sequence numbers remain
    global even when only one run is loaded.
    """
    from smartapply.pipeline.generation.offer_resume import resumable_generation_job_ids

    selected_ids = None if run_ids is None else tuple(dict.fromkeys(run_ids))
    if selected_ids == ():
        return {"runs": [], "reserved_job_ids": [], "resumable_job_ids": [],
                "active_run_count": 0, "active_run_id": ""}
    with session_scope() as session:
        # Column tuples deliberately exclude profile_data, request_body and all
        # response_data. Periodic UI reads do not copy candidate documents.
        runs_query = select(
            GenerationRun.id, GenerationRun.mode, GenerationRun.state, GenerationRun.routes,
            GenerationRun.cancel_requested, GenerationRun.last_error,
            GenerationRun.created_at, GenerationRun.updated_at,
        ).order_by(GenerationRun.created_at.desc(), GenerationRun.id.desc())
        items_query = select(
            GenerationItem.id, GenerationItem.run_id, GenerationItem.job_id,
            GenerationItem.title, GenerationItem.company, GenerationItem.state,
            GenerationItem.application_id, GenerationItem.error,
        )
        calls_query = select(
            GenerationCall.item_id, GenerationCall.position, GenerationCall.purpose,
            GenerationCall.state, GenerationCall.wave_id,
        ).order_by(GenerationCall.item_id, GenerationCall.position)
        contexts_query = select(
            GenerationItemContext.item_id, GenerationItemContext.routes,
        )
        waves_query = select(
            GenerationWave.id, GenerationWave.state, GenerationWave.provider_data,
            GenerationWave.provider_batch_id,
        )
        if selected_ids is not None:
            runs_query = runs_query.where(GenerationRun.id.in_(selected_ids))
            items_query = items_query.where(GenerationItem.run_id.in_(selected_ids))
            calls_query = calls_query.join(GenerationItem).where(
                GenerationItem.run_id.in_(selected_ids),
            )
            contexts_query = contexts_query.join(GenerationItem).where(
                GenerationItem.run_id.in_(selected_ids),
            )
            waves_query = waves_query.where(GenerationWave.run_id.in_(selected_ids))
        runs = session.execute(runs_query).all()
        items = session.execute(items_query).all()
        calls = session.execute(calls_query).all()
        context_routes = dict(session.execute(contexts_query).all())
        waves = session.execute(waves_query).all()
        if selected_ids is None:
            sequence_by_id = {run.id: sequence for sequence, run in enumerate(reversed(runs), 1)}
        else:
            # IDs alone preserve the historical numbering/order without loading
            # other runs' routes or their item/call/context/wave payloads.
            ordered_ids = list(session.scalars(select(GenerationRun.id).order_by(
                GenerationRun.created_at.desc(), GenerationRun.id.desc(),
            ))) if runs else []
            sequence_by_id = {run_id: sequence for sequence, run_id in enumerate(reversed(ordered_ids), 1)}
        if not include_availability:
            resumable_ids = set()
        elif selected_ids is None:
            resumable_ids = resumable_generation_job_ids(session)
        else:
            resumable_ids = resumable_generation_job_ids(session, job_ids=[item.job_id for item in items])
    by_run, by_item = defaultdict(list), defaultdict(list)
    for item in items:
        by_run[item.run_id].append(item)
    for call in calls:
        by_item[call.item_id].append(call)
    wave_by_id = {wave.id: wave for wave in waves}
    summaries = []
    reserved_ids = []
    for run in reversed(runs):
        sequence = sequence_by_id[run.id]
        run_items = by_run[run.id]
        phase_calls = defaultdict(list)
        item_phase = {}
        for item in run_items:
            review_seen = False
            for call in by_item[item.id]:
                phase = call.purpose
                if phase == "document_review":
                    if review_seen:
                        phase = "confirmation"
                    review_seen = True
                phase_calls[phase].append(call)
                item_phase[item.id] = phase
            if item.state == "completed":
                item_phase[item.id] = "rendering"
        phases = []
        total = len(run_items)
        for phase, label in PHASES:
            pcalls = phase_calls[phase]
            if phase in OPTIONAL and not pcalls:
                continue
            python = phase in {"assembly", "rendering"}
            if python:
                completed = sum(
                    item.state == "completed" if phase == "rendering"
                    else any(call.purpose == "application_texts" for call in by_item[item.id])
                    for item in run_items
                )
                n, failed = total, 0
                status = "completed" if completed == n else "waiting"
                batches = []
            else:
                n = len(pcalls)
                completed = sum(call.state == "completed" for call in pcalls)
                failed = sum(call.state == "failed" for call in pcalls)
                if any(call.state in {"uncertain", "submitting"} for call in pcalls):
                    status = "attention" if run.state == "attention" else "submitting"
                elif any(call.state == "submitted" for call in pcalls):
                    status = "in_progress"
                elif any(call.state == "prepared" for call in pcalls):
                    status = "preparing"
                elif n and completed + failed == n:
                    # All offers that reached this branch have a response. A
                    # failed branch is visible even if other offers progressed.
                    status = "partial" if failed else "completed"
                else:
                    status = "waiting"
                wave_ids = list(dict.fromkeys(call.wave_id for call in pcalls if call.wave_id))
                batches = [{
                    "id": wave_id, "number": number, "status": wave_by_id[wave_id].state,
                    "status_label": wave_by_id[wave_id].state,
                    "provider_batch_id": wave_by_id[wave_id].provider_batch_id or "",
                    "total": sum(call.wave_id == wave_id for call in pcalls),
                    "completed": sum(call.wave_id == wave_id and call.state == "completed" for call in pcalls),
                    "failed": sum(call.wave_id == wave_id and call.state == "failed" for call in pcalls),
                } for number, wave_id in enumerate(wave_ids, 1) if wave_id in wave_by_id]
            phases.append({
                "phase": phase, "label": label, "title": label,
                "execution_kind": "python" if python else run.mode,
                "status": status, "total": n, "completed": completed, "failed": failed,
                "batches": batches, "batch_count": len(batches),
            })
        current = next((phase["phase"] for phase in phases
                        if phase["status"] not in {"completed", "skipped"}), "done")
        state = "cancelling" if run.cancel_requested and run.state == "active" else run.state
        generated = sum(item.state == "completed" for item in run_items)
        failed = sum(item.state == "failed" for item in run_items)
        skipped = sum(item.state in {"stale", "transferred"} for item in run_items)
        uncertain = any(call.state in {"uncertain", "submitting"}
                        for item in run_items for call in by_item[item.id])
        effective_routes = [route for item in run_items
                            for route in context_routes.get(item.id, run.routes).values()]
        providers = list(dict.fromkeys(route["provider"] for route in effective_routes))
        models = list(dict.fromkeys(route["model"] for route in effective_routes))
        if include_availability and run.state in RESERVED_GENERATION_STATES:
            reserved_ids.extend(item.job_id for item in run_items
                                if item.state not in TERMINAL_GENERATION_ITEM_STATES)
        summaries.append({
            "id": run.id, "sequence": sequence, "kind": "generation", "mode": run.mode,
            "status": state, "status_label": "Arrêt en cours" if state == "cancelling"
            else STATUS_LABELS.get(state, state),
            "phase": "done" if run.state == "completed" else current,
            "phase_label": dict(PHASES).get(current, "Documents prêts"),
            "is_active": run.state in {"active", "attention", "blocked"},
            "can_cancel": run.state in {"active", "attention", "blocked"} and not run.cancel_requested,
            "can_retry": run.state in {"paused", "partial", "failed", "blocked", "attention", "cancelled"}
            and (failed > 0 or generated + skipped < total)
            and not (run.mode == "immediate" and uncertain),
            "can_abandon": run.state in {"paused", "partial", "failed"}
            or (run.mode == "immediate" and run.state == "attention"),
            "total": total, "generated": generated, "analyzed": generated, "failed": failed,
            "skipped": skipped, "phase_batches": phases,
            "created_at": _date(run.created_at), "updated_at": _date(run.updated_at),
            "last_error": run.last_error,
            "provider_label": " · ".join(providers), "model": " · ".join(models),
            "items": [{
                "id": item.job_id, "title": item.title, "company": item.company,
                "application_id": item.application_id, "state": item.state,
                "state_label": "Documents prêts" if item.state == "completed"
                else "Repris dans un autre parcours" if item.state == "transferred"
                else item.error or dict(PHASES).get(item_phase.get(item.id), "En attente"),
            } for item in run_items],
            "errors": [{"job_id": item.job_id, "title": item.title, "message": item.error}
                       for item in run_items if item.error],
        })
    summaries.reverse()
    return {"runs": summaries, "reserved_job_ids": sorted(set(reserved_ids)),
            "resumable_job_ids": sorted(resumable_ids),
            "active_run_count": sum(run["is_active"] for run in summaries),
            "active_run_id": next((run["id"] for run in summaries if run["is_active"]),
                                  summaries[0]["id"] if summaries else "")}
