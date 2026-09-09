"""Developer command-line interface for Élan."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from smartapply.config import get_settings
from smartapply.cv.selector import CvBlockSelector
from smartapply.database import init_db as _init_db
from smartapply.database import session_scope
from smartapply.database.repository import (
    list_applications,
    list_jobs,
    total_cost,
    update_application_tracking,
)
from smartapply.jobsearch import (
    APPLICATION_STATUSES,
    STATUS_LABELS,
    next_action_for,
)
from smartapply.logging_setup import setup_logging
from smartapply.profile import get_profile
from smartapply.ranking import build_profile_text, get_embeddings_provider
from smartapply.scrapers import SERPAPI_DATE_POSTED_OPTIONS

DATE_POSTED_CHOICE = click.Choice(list(SERPAPI_DATE_POSTED_OPTIONS))
SCRAPER_SOURCE_CHOICE = click.Choice(["serpapi", "francetravail", "linkedin", "welcometothejungle"])


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"", "none", "all", "unlimited"}:
        return None
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise click.BadParameter("expected an integer or one of: none, all, unlimited") from exc
    if parsed < 0:
        raise click.BadParameter("must be >= 0")
    return parsed


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Élan — outils de développement et de maintenance."""
    setup_logging()


@cli.command("init-db")
def init_db_command() -> None:
    """Create the SQLite tables."""
    _init_db()
    click.echo(f"Database initialized at {get_settings().database_url}")


@cli.command("refresh-embeddings")
def refresh_embeddings_command() -> None:
    """Precompute and cache the stable profile and project embeddings."""
    _init_db()
    profile = get_profile()
    texts = [build_profile_text(profile)]
    texts.extend(CvBlockSelector.project_text(project) for project in profile.projects)
    provider = get_embeddings_provider()
    provider.embed(texts)
    click.echo(
        json.dumps(
            {
                "provider": provider.name,
                "model": provider.model_name,
                "profile_embeddings": 1,
                "project_embeddings": len(profile.projects),
                "cached": True,
            },
            indent=2,
        )
    )


@cli.command("sync-companies")
@click.option(
    "--csv",
    "csv_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="CSV des entreprises à synchroniser dans la base configurée d’Élan.",
)
@click.option("--dry-run", is_flag=True, help="Afficher les changements sans modifier les données.")
def sync_companies_command(csv_path: Path, dry_run: bool) -> None:
    """Synchroniser les entreprises sans écraser les fiches checked ni supprimer de lignes."""
    import csv
    import sqlite3
    from datetime import datetime, timezone

    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    from smartapply.database.repository.companies import sync_companies

    try:
        report = sync_companies(csv_path, dry_run=True)
        backup = None
        if not dry_run:
            url = make_url(get_settings().database_url)
            if (report["added"] or report["updated"]) and (
                url.get_backend_name() == "sqlite" and url.database not in {None, ":memory:"}
            ):
                database = Path(url.database).expanduser().resolve()
                if database.exists():
                    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    backup_dir = database.parent / "backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup = backup_dir / f"{database.stem}-companies-{timestamp}.db"
                    with (
                        sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as source,
                        sqlite3.connect(backup) as destination,
                    ):
                        source.backup(destination)
                    backup.chmod(0o600)
            report = sync_companies(csv_path)
        click.echo(
            json.dumps(
                {
                    "csv": str(csv_path.resolve()),
                    "database": get_settings().database_url,
                    "dry_run": dry_run,
                    **report,
                    "backup": str(backup) if backup else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except (OSError, ValueError, csv.Error, sqlite3.Error, SQLAlchemyError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("sync-profile")
@click.option(
    "--direction",
    type=click.Choice(["to-app", "from-app"]),
    default="to-app",
    show_default=True,
    help="Copy the project profile to the app, or the app profile to the project.",
)
@click.option(
    "--project-profile",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("smartapply/profile/data"),
    show_default=True,
    help="Project profile directory, relative to the current working directory.",
)
@click.option(
    "--app-profile",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="App profile directory; defaults to ELAN_HOME/profile (independent of PROFILE_DIR).",
)
@click.option("--dry-run", is_flag=True, help="Compare and validate without copying any files.")
def sync_profile_command(
    direction: str,
    project_profile: Path,
    app_profile: Path | None,
    dry_run: bool,
) -> None:
    """Copy profile JSON files with validation and an automatic destination backup.

    The source wins for each copied file, including its list of projects.
    Optional destination files absent from the source are kept. Credentials,
    databases and generated documents are never copied.
    """
    from smartapply.config import RUNTIME_DIR
    from smartapply.profile.sync import sync_profile

    app_profile = app_profile if app_profile is not None else RUNTIME_DIR / "profile"
    source, destination = (
        (project_profile, app_profile) if direction == "to-app" else (app_profile, project_profile)
    )
    try:
        result = sync_profile(source, destination, dry_run=dry_run)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Source: {result.source}")
    click.echo(f"Destination: {result.destination}")
    for label, names in (
        ("Create", result.created),
        ("Replace", result.updated),
        ("Unchanged", result.unchanged),
        ("Keep destination-only", result.retained),
    ):
        if names:
            click.echo(f"{label}: {', '.join(names)}")
    if dry_run:
        click.echo("Preview only; no files written.")
    elif result.backup:
        click.echo(f"Synchronized. Backup: {result.backup}")
    else:
        click.echo("Profiles already synchronized for all source files.")


@cli.command("ingest")
@click.option("--source", required=True, type=SCRAPER_SOURCE_CHOICE)
@click.option("--query", "-q", required=True)
@click.option("--location", "-l", default=None)
@click.option("--max-results", callback=lambda _, __, value: _optional_int(value), default="20")
@click.option("--date-posted", type=DATE_POSTED_CHOICE, default=None)
@click.option("--serpapi-hl", default=None, help="Google Jobs language(s), e.g. en, fr or en,fr.")
def ingest_command(
    source: str,
    query: str,
    location: str | None,
    max_results: int | None,
    date_posted: str | None,
    serpapi_hl: str | None,
) -> None:
    """Scrape one source and persist new jobs."""
    from smartapply.pipeline import Pipeline
    from smartapply.pipeline.pipeline import freshness_kwargs

    if source == "serpapi" and max_results is None:
        raise click.BadParameter(
            f"{source} requires a bounded --max-results value.",
            param_hint="--max-results",
        )
    if source == "linkedin" and max_results is None:
        raise click.BadParameter(
            "LinkedIn has no unlimited mode. Set --max-results below LINKEDIN_MAX_RESULTS.",
            param_hint="--max-results",
        )

    p = Pipeline()
    kwargs = freshness_kwargs(source, date_posted=date_posted, serpapi_hl=serpapi_hl)
    report = p.ingest(source, query, location, max_results=max_results, **kwargs)
    click.echo(json.dumps(report.__dict__, indent=2, default=str))


@cli.command("ingest-url")
@click.option("--url", "-u", required=True)
def ingest_url_command(url: str) -> None:
    from smartapply.pipeline import Pipeline

    report = Pipeline().ingest_url(url)
    click.echo(json.dumps(report.__dict__, indent=2, default=str))


@cli.command("ingest-text")
@click.option("--title", required=True)
@click.option("--company", required=True)
@click.option("--location", default=None)
@click.option("--application-url", default=None)
@click.option("--file", "text_file", type=click.Path(exists=True))
def ingest_text_command(
    title: str,
    company: str,
    location: str | None,
    application_url: str | None,
    text_file: str | None,
) -> None:
    """Ingest a job from raw text (paste via stdin or --file)."""
    from smartapply.pipeline import Pipeline

    text = Path(text_file).read_text() if text_file else sys.stdin.read()
    report = Pipeline().ingest_text(
        text,
        title=title,
        company=company,
        location=location,
        application_url=application_url,
    )
    click.echo(json.dumps(report.__dict__, indent=2, default=str))


@cli.command("process")
@click.option("--top-k", type=int, default=None)
def process_command(top_k: int | None) -> None:
    """Filter, rank and analyze pending jobs."""
    from smartapply.pipeline import Pipeline

    report = Pipeline().process_pending(top_k_analyze=top_k)
    click.echo(json.dumps(report.__dict__, indent=2))


@cli.command("apply")
@click.option("--job-id", type=int, required=True)
@click.option("--form-url", default=None, help="Application form URL if known.")
def apply_command(
    job_id: int,
    form_url: str | None,
) -> None:
    """Generate the CV and motivation letter for one analyzed job."""
    from smartapply.pipeline import Pipeline

    report = Pipeline().apply_to(
        job_id,
        form_url=form_url,
    )
    click.echo(json.dumps(report.__dict__, indent=2, default=str))


@cli.command("pipeline")
@click.option("--source", "sources", multiple=True, required=True, type=SCRAPER_SOURCE_CHOICE)
@click.option("--query", "-q", required=True)
@click.option("--location", "-l", default=None)
@click.option("--max-per-source", callback=lambda _, __, value: _optional_int(value), default="20")
@click.option("--top-apply", type=int, default=5)
@click.option("--date-posted", type=DATE_POSTED_CHOICE, default=None)
@click.option("--serpapi-hl", default=None, help="Google Jobs language(s), e.g. en, fr or en,fr.")
def pipeline_command(
    sources: tuple[str, ...],
    query: str,
    location: str | None,
    max_per_source: int | None,
    top_apply: int,
    date_posted: str | None,
    serpapi_hl: str | None,
) -> None:
    """End-to-end: ingest from sources → process → apply to top K."""
    from smartapply.pipeline import Pipeline

    if "serpapi" in sources and max_per_source is None:
        raise click.BadParameter(
            "SerpApi requires a bounded --max-per-source value.",
            param_hint="--max-per-source",
        )

    p = Pipeline()
    source_tuples = [(name, query, location) for name in sources]
    report = p.run_end_to_end(
        sources=source_tuples,
        max_per_source=max_per_source,
        top_k_apply=top_apply,
        date_posted=date_posted,
        serpapi_hl=serpapi_hl,
    )
    click.echo(json.dumps(report, indent=2, default=str))


@cli.command("list-jobs")
@click.option("--status", default=None)
@click.option("--limit", type=int, default=50)
def list_jobs_command(status: str | None, limit: int) -> None:
    with session_scope() as s:
        jobs = list_jobs(s, status=status, limit=limit)
        rows = [
            {
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "source": j.source,
                "status": j.status,
                "score": j.score.final_score if j.score else None,
            }
            for j in jobs
        ]
    click.echo(json.dumps(rows, indent=2))


@cli.command("list-applications")
def list_applications_command() -> None:
    with session_scope() as s:
        apps = list_applications(s)
        rows = [
            {
                "id": a.id,
                "job_id": a.job_id,
                "status": a.status,
                "form_url": a.form_submission_url,
                "form_submitted_at": a.form_submitted_at,
                "next_action": next_action_for(a.status, a.updated_at),
                "cv_docx": a.cv_docx_path,
                "cv_pdf": a.cv_pdf_path,
                "motivation_letter_pdf": next(
                    (doc.path for doc in a.documents if doc.doc_type == "motivation_letter_pdf"),
                    None,
                ),
                "notes": a.notes,
            }
            for a in apps
        ]
    click.echo(json.dumps(rows, indent=2, default=str))


@cli.command("update-application")
@click.option("--application-id", type=int, required=True)
@click.option(
    "--status",
    type=click.Choice(APPLICATION_STATUSES),
    default=None,
    help="Nouveau statut de suivi.",
)
@click.option("--notes", default=None, help="Notes de suivi ou prochaine action.")
@click.option(
    "--form-submitted",
    is_flag=True,
    help="Marque le formulaire ATS comme soumis (timestamp + auto-promotion).",
)
def update_application_command(
    application_id: int,
    status: str | None,
    notes: str | None,
    form_submitted: bool,
) -> None:
    """Update application follow-up status, notes, and submission state."""
    if status is None and notes is None and not form_submitted:
        raise click.UsageError("Provide --status, --notes and/or --form-submitted.")
    with session_scope() as s:
        app = update_application_tracking(
            s,
            application_id,
            status=status,
            notes=notes,
            form_submitted=form_submitted,
        )
        row = {
            "id": app.id,
            "job_id": app.job_id,
            "status": app.status,
            "status_label": STATUS_LABELS.get(app.status, app.status),
            "form_submitted_at": app.form_submitted_at,
            "next_action": next_action_for(app.status, app.updated_at),
            "notes": app.notes,
        }
    click.echo(json.dumps(row, indent=2, default=str))


@cli.command("stats")
def stats_command() -> None:
    """Display pipeline statistics — counts per status and total cost."""
    with session_scope() as s:
        all_jobs = list_jobs(s)
        per_status: dict[str, int] = {}
        for j in all_jobs:
            per_status[j.status] = per_status.get(j.status, 0) + 1
        cost = total_cost(s)
    click.echo(
        json.dumps(
            {
                "total_jobs": len(all_jobs),
                "per_status": per_status,
                "total_llm_cost_usd": round(cost, 4),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    cli()
