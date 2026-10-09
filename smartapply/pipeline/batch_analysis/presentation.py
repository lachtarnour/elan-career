"""Read execution progress without loading persisted prompts or offer bodies."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import load_only

from smartapply.database import session_scope
from smartapply.database.analysis_batches import (
    ACTIVE_ANALYSIS_RUN_STATES,
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.display_metadata import display_metadata_current
from smartapply.database.models import Job
from smartapply.jobsearch.status import display_status
from smartapply.pipeline.process.analysis_admission import analysis_queue_job_ids

STATUS_LABELS = {
    "preparing": "Préparation des demandes",
    "prepared": "Préparation des demandes",
    "submitting": "Envoi des demandes",
    "validating": "Validation des demandes",
    "in_progress": "Traitement en cours",
    "finalizing": "Préparation des résultats",
    "completed": "Terminé",
    "imported": "Résultats enregistrés",
    "failed": "Échec",
    "expired": "Délai dépassé",
    "cancelling": "Annulation en cours",
    "cancelled": "Annulé",
    "attention": "Envoi à vérifier",
    "blocked": "Connexion à rétablir",
    "partial": "Terminé avec des demandes restantes",
    "waiting": "En attente de l’étape précédente",
    "skipped": "Non exécutée",
}


def _date(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _experience(job):
    metadata = job.display_metadata
    years = metadata.get("required_min_years") if display_metadata_current(metadata) else None
    return f"{years}+ ans" if isinstance(years, (int, float)) else "—"


def _active_status(waves):
    states = [wave.provider_data.get("status", wave.state) for wave in waves]
    for state in (
        "attention",
        "submitting",
        "prepared",
        "validating",
        "in_progress",
        "finalizing",
        "cancelling",
        "failed",
        "expired",
        "cancelled",
        "completed",
    ):
        if state in states:
            return state
    return states[-1]


def _phase(phase, waves, items, active_phase):
    filtered = {"filtered_after_screening"}
    selected = [wave for wave in waves if wave.phase == phase]
    batches = [
        {
            "number": index,
            "status": wave.provider_data.get("status", wave.state),
            "status_label": STATUS_LABELS.get(
                wave.provider_data.get("status", wave.state), wave.state
            ),
            "offers": wave.provider_data.get("offers"),
            "total": wave.provider_data.get("total", 0),
            "completed": wave.provider_data.get("completed", 0),
            "failed": wave.provider_data.get("failed", 0),
        }
        for index, wave in enumerate(selected, start=1)
    ]
    # Older journals stored only request totals. A single one-to-one wave can
    # recover its offer count without loading request bodies from the journal.
    if len(batches) == 1 and batches[0]["offers"] is None and batches[0]["total"] == len(items):
        batches[0]["offers"] = len(items)
    if selected:
        active = [wave for wave in selected if wave.state != "imported"]
        if active:
            status = _active_status(active)
        else:
            states = [wave.provider_data.get("status", wave.state) for wave in selected]
            status = next(
                (state for state in states if state in {"failed", "expired", "cancelled"}),
                "completed",
            )
        if (
            phase == "evaluation"
            and status == "completed"
            and not any(w.provider_batch_id for w in selected)
            and not any(w.provider_data.get("completed", 0) for w in selected)
            and not any(
                item.state in {"evaluation_pending", "evaluation_failed", "completed"}
                for item in items
            )
        ):
            status = "skipped"
        # Provider request counters, including retry attempts; these are not offer counts.
        return {
            "phase": phase,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "total": sum(w.provider_data.get("total", 0) for w in selected),
            "completed": sum(w.provider_data.get("completed", 0) for w in selected),
            "failed": sum(w.provider_data.get("failed", 0) for w in selected),
            "batch_count": len(batches),
            "submitted_count": sum(bool(w.provider_batch_id) for w in selected),
            "batches": batches,
        }
    evaluation_states = {"evaluation_pending", "evaluation_failed", "completed"}
    if phase != "screening" and items and all(item.state in filtered for item in items):
        status = "skipped"
    else:
        completed = (
            all(item.state in evaluation_states | filtered for item in items)
            if phase == "extraction"
            else all(
                item.state
                in evaluation_states | filtered | {"extraction_pending", "extraction_failed"}
                for item in items
            )
            if phase == "screening"
            else all(item.state in {"completed"} | filtered for item in items)
        )
        status = "completed" if completed else "preparing" if phase == active_phase else "waiting"
    return {
        "phase": phase,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "total": 0,
        "completed": 0,
        "failed": 0,
        "batch_count": 0,
        "submitted_count": 0,
        "batches": [],
    }


def _item_label(item, phase, status):
    if item.state == f"{phase}_pending" and status in {"in_progress", "finalizing"}:
        return "En extraction" if phase == "extraction" else "En évaluation"
    return {
        "extraction_pending": "À extraire",
        "screening_pending": "À préfiltrer",
        "filtered_after_screening": "Écartée au préfiltre",
        "evaluation_pending": "À évaluer",
        "completed": "Analysée",
        "extraction_failed": "Extraction en échec",
        "evaluation_failed": "Évaluation en échec",
        "skipped": "Non analysée",
        "cancelled": "Annulée",
    }.get(item.state, "En attente")


def _run(run, waves, items, job_details, sequence, scope_enabled=False):
    active_waves = [wave for wave in waves if wave.state != "imported"]
    active = run.state in {"active", "attention"}
    if active_waves:
        phase = active_waves[0].phase
        status = _active_status(active_waves)
    else:
        phase = (
            "screening"
            if any(item.state == "screening_pending" for item in items)
            else "extraction"
            if any(item.state == "extraction_pending" for item in items)
            else "evaluation"
        )
        status = "preparing"
    if run.state == "blocked":
        status = "blocked"
    elif not active:
        phase = "done"
        status = run.state
    elif run.state == "attention":
        status = "attention"
    elif run.cancel_requested:
        status = "cancelling"
    failed = [item for item in items if item.state.endswith("_failed")]
    skipped = [item for item in items if item.state == "skipped"]
    screened_out = [item for item in items if item.state in {"filtered_after_screening"}]
    deadlines = [
        wave.provider_data.get("expires_at")
        for wave in active_waves
        if wave.provider_data.get("expires_at")
    ]
    return {
        "id": run.id,
        "provider": run.provider,
        "provider_label": "Anthropic" if run.provider == "anthropic" else "OpenAI",
        "model": run.model,
        "sequence": sequence,
        "phase": phase,
        "phase_label": {
            "screening": "Étape 1/3 · Préfiltre des missions",
            "extraction": "Étape 2/3 · Extraction des offres"
            if scope_enabled
            else "Étape 1/2 · Extraction des offres",
            "evaluation": "Étape 3/3 · Évaluation de la pertinence"
            if scope_enabled
            else "Étape 2/2 · Évaluation de la pertinence",
            "done": "Bilan de l’analyse",
        }[phase],
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "total": len(items),
        "completed": sum(item.state == "completed" for item in items),
        "analyzed": sum(item.state == "completed" for item in items),
        "screened_out": len(screened_out),
        "failed": len(failed),
        "skipped": len(skipped),
        "created_at": _date(run.created_at),
        "updated_at": _date(run.updated_at),
        "completed_at": _date(run.completed_at),
        "expires_at": _date(min(deadlines)) if deadlines else "",
        "last_error": run.last_error,
        "is_active": active,
        "can_retry": (
            (scope_enabled and bool(failed) and not active and not run.cancel_requested)
            or run.state in {"attention", "blocked"}
        ),
        "can_cancel": run.state in ACTIVE_ANALYSIS_RUN_STATES and not run.cancel_requested,
        "errors": [
            {"job_id": item.job_id, "title": item.title, "message": item.error}
            for item in [*failed, *skipped]
        ],
        "items": [
            {
                "id": item.job_id,
                "title": item.title,
                "company": item.company,
                "location": job_details.get(item.job_id, {}).get("location", ""),
                "experience": job_details.get(item.job_id, {}).get("experience", "—"),
                "source": job_details.get(item.job_id, {}).get("source", ""),
                "published_date": job_details.get(item.job_id, {}).get("published_date", ""),
                "offer_status": job_details.get(item.job_id, {}).get("status", "scraped"),
                "state": item.state,
                "state_label": _item_label(item, phase, status),
            }
            for item in items
        ],
        "phase_batches": [
            _phase(listed_phase, waves, items, phase if active else "done")
            for listed_phase in (
                ("screening", "extraction", "evaluation")
                if scope_enabled
                else ("extraction", "evaluation")
            )
        ],
    }


def analysis_workspace() -> dict:
    with session_scope() as session:
        scope_runs = set(
            session.scalars(
                select(AnalysisBatchRun.id).where(AnalysisBatchRun.scope_context.is_not(None))
            )
        )
        runs = list(
            session.scalars(
                select(AnalysisBatchRun)
                .options(
                    load_only(
                        AnalysisBatchRun.id,
                        AnalysisBatchRun.provider,
                        AnalysisBatchRun.model,
                        AnalysisBatchRun.state,
                        AnalysisBatchRun.cancel_requested,
                        AnalysisBatchRun.last_error,
                        AnalysisBatchRun.created_at,
                        AnalysisBatchRun.updated_at,
                        AnalysisBatchRun.completed_at,
                    )
                )
                .order_by(AnalysisBatchRun.created_at.desc(), AnalysisBatchRun.id.desc())
            )
        )
        sequences = {run.id: len(runs) - index for index, run in enumerate(runs)}
        runs = [run for run in runs if run.state != "cancelled"]
        visible_ids = [run.id for run in runs]
        items = list(
            session.scalars(
                select(AnalysisBatchItem)
                .where(AnalysisBatchItem.run_id.in_(visible_ids))
                .options(
                    load_only(
                        AnalysisBatchItem.id,
                        AnalysisBatchItem.run_id,
                        AnalysisBatchItem.job_id,
                        AnalysisBatchItem.title,
                        AnalysisBatchItem.company,
                        AnalysisBatchItem.state,
                        AnalysisBatchItem.error,
                    )
                )
            )
        )
        waves = list(
            session.scalars(
                select(AnalysisBatchWave)
                .where(AnalysisBatchWave.run_id.in_(visible_ids))
                .options(
                    load_only(
                        AnalysisBatchWave.id,
                        AnalysisBatchWave.run_id,
                        AnalysisBatchWave.phase,
                        AnalysisBatchWave.state,
                        AnalysisBatchWave.provider_data,
                        AnalysisBatchWave.created_at,
                    )
                )
                .order_by(AnalysisBatchWave.created_at, AnalysisBatchWave.id)
            )
        )
        active_ids = {run.id for run in runs if run.state in ACTIVE_ANALYSIS_RUN_STATES}
        available_ids = analysis_queue_job_ids(session)
        jobs = list(
            session.scalars(
                select(Job)
                .options(
                    load_only(
                        Job.id,
                        Job.title,
                        Job.company,
                        Job.location,
                        Job.source,
                        Job.display_metadata,
                        Job.status,
                        Job.published_date,
                    ),
                )
                .where(Job.id.in_(set(available_ids) | {item.job_id for item in items}))
                .order_by(Job.published_date.desc(), Job.id.desc())
            )
        )
        available_set = set(available_ids)
        available_jobs = [job for job in jobs if job.id in available_set]
        checkpoints = set(
            session.scalars(
                select(AnalysisCheckpoint.job_id).where(
                    AnalysisCheckpoint.job_id.in_(available_ids)
                )
            )
        )
        job_details = {
            job.id: {
                "location": job.location or "",
                "status": display_status(job.status),
                "experience": _experience(job),
                "source": job.source,
                "published_date": _date(job.published_date),
            }
            for job in jobs
        }
        candidates = [
            {
                "id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location or "",
                "experience": _experience(job),
                "source": job.source,
                "offer_status": display_status(job.status),
                "state": "evaluation_pending" if job.id in checkpoints else "extraction_pending",
                "state_label": "À évaluer" if job.id in checkpoints else "À extraire",
                "published_date": _date(job.published_date),
            }
            for job in available_jobs
        ]
        items_by_run, waves_by_run = defaultdict(list), defaultdict(list)
        for item in items:
            items_by_run[item.run_id].append(item)
        for wave in waves:
            waves_by_run[wave.run_id].append(wave)
        summaries = [
            _run(
                run,
                waves_by_run[run.id],
                items_by_run[run.id],
                job_details,
                sequences[run.id],
                run.id in scope_runs,
            )
            for run in runs
        ]
        return {
            "candidates": candidates,
            "candidates_count": len(candidates),
            "pending_count": len(candidates),
            "runs": summaries,
            "active_run_id": next((run.id for run in runs if run.id in active_ids), ""),
            "active_run_count": len(active_ids),
        }
