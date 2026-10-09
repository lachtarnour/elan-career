"""Copy the installed runtime for source development, leaving the original untouched."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

PROJECT = Path(__file__).resolve().parents[1]
PATH_COLUMNS = (
    ("applications", "cv_docx_path"),
    ("applications", "cv_pdf_path"),
    ("generated_documents", "path"),
)


def clone_runtime(source_home: Path, destination: Path, development_env: Path) -> dict:
    source_home = source_home.expanduser().resolve()
    destination = destination.expanduser().resolve()
    development_env = development_env.expanduser().resolve()
    if (destination == source_home or destination.is_relative_to(source_home)
            or source_home.is_relative_to(destination)
            or development_env.is_relative_to(source_home)):
        raise ValueError("La copie et le .env de développement doivent être hors du runtime installé.")
    if destination.exists():
        raise FileExistsError(f"Copie déjà présente, aucun écrasement : {destination}")
    original_env = development_env.read_text(encoding="utf-8")
    source_settings = dotenv_values(source_home / ".env")
    url = make_url(source_settings["DATABASE_URL"])
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Le runtime source doit utiliser un fichier SQLite existant.")
    source_db = Path(url.database).expanduser()
    if not source_db.is_absolute():
        source_db = source_home / source_db
    source_db = source_db.resolve(strict=True)
    directories = {}
    for key, name in (("PROFILE_DIR", "profile"), ("OUTPUT_DIR", "documents"), ("CACHE_DIR", "cache")):
        path = Path(source_settings.get(key) or source_home / name).expanduser()
        directories[name] = (path if path.is_absolute() else source_home / path).resolve()
    # Reject an unsafe destination before copying any files or changing configuration.
    for path in (source_db.parent, *directories.values()):
        if destination == path or destination.is_relative_to(path) or path.is_relative_to(destination):
            raise ValueError(f"Le dossier de copie recoupe les données source : {path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    target_db = destination / "data" / "smartapply.db"
    target_db.parent.mkdir()
    # SQLite backup includes committed WAL transactions and gives a consistent snapshot.
    with (
        closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(target_db)) as target,
    ):
        source.backup(target)
    for name, path in directories.items():
        target = destination / name
        if path.exists():
            shutil.copytree(path, target)
        else:
            target.mkdir()
    target_db.chmod(0o600)
    counts = {}
    relocated = 0
    old_root = str(directories["documents"]) + "/"
    new_root = str(destination / "documents") + "/"
    with closing(sqlite3.connect(target_db)) as target, target:
        for table, column in PATH_COLUMNS:
            rows = target.execute(f'SELECT id, "{column}" FROM "{table}"').fetchall()
            for row_id, value in rows:
                if value and value.startswith(old_root):
                    target.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE id = ?',
                        (new_root + value[len(old_root):], row_id),
                    )
                    relocated += 1
                elif value:
                    raise ValueError(f"Document hors du dossier source : {table}.{column}")
        for table in ("jobs", "job_analyses", "applications", "generated_documents"):
            counts[table] = target.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("La copie SQLite a échoué au contrôle d’intégrité.")
    replacements = {
        "DATABASE_URL": f"sqlite:///{target_db}",
        "PROFILE_DIR": str(destination / "profile"),
        "OUTPUT_DIR": str(destination / "documents"),
        "CACHE_DIR": str(destination / "cache"),
    }
    lines = []
    seen = set()
    for line in original_env.splitlines():
        key = line.removeprefix("export ").split("=", 1)[0].strip()
        if key in replacements:
            lines.append(f"{key}='{replacements[key]}'")
            seen.add(key)
        else:
            lines.append(line)
    lines.extend(f"{key}='{value}'" for key, value in replacements.items() if key not in seen)
    # Keep an exact private copy of the previous configuration for a reversible switch.
    backup = destination / ".env.before-copy"
    backup.write_text(original_env, encoding="utf-8")
    backup.chmod(0o600)
    manifest = {"source_database": str(source_db), "database": str(target_db),
                "counts": counts, "relocated_document_paths": relocated}
    (destination / "copy-report.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )
    with tempfile.NamedTemporaryFile(mode="w", dir=development_env.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write("\n".join(lines) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, development_env)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", type=Path,
                        default=Path.home() / "Library" / "Application Support" / "Elan")
    parser.add_argument("--destination", type=Path, default=PROJECT / "data" / "development-runtime")
    parser.add_argument("--development-env", type=Path, default=PROJECT / ".env")
    args = parser.parse_args()
    print(json.dumps(clone_runtime(args.source_home, args.destination, args.development_env), indent=2))


if __name__ == "__main__":
    main()
