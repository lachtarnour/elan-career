"""A source runtime copy must preserve data and isolate document writes."""

import sqlite3
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scripts.clone_development_runtime import clone_runtime


def source_runtime(tmp_path: Path):
    source = tmp_path / "installed"
    (source / "data").mkdir(parents=True)
    (source / "profile").mkdir()
    (source / "documents").mkdir()
    (source / "cache").mkdir()
    (source / "profile" / "profile.json").write_text('{"name":"candidate"}')
    document = source / "documents" / "cv.pdf"
    document.write_bytes(b"original PDF")
    database = source / "data" / "smartapply.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE job_analyses (id INTEGER PRIMARY KEY, result TEXT);
        CREATE TABLE applications (id INTEGER PRIMARY KEY, cv_docx_path TEXT, cv_pdf_path TEXT);
        CREATE TABLE generated_documents (id INTEGER PRIMARY KEY, path TEXT);
    """)
    connection.execute("INSERT INTO jobs VALUES (1, 'offer preserved')")
    connection.execute("INSERT INTO job_analyses VALUES (1, 'analysis preserved')")
    connection.execute("INSERT INTO applications VALUES (1, NULL, ?)", (str(document),))
    connection.execute("INSERT INTO generated_documents VALUES (1, ?)", (str(document),))
    connection.commit()
    (source / ".env").write_text(f"DATABASE_URL='sqlite:///{database}'\n")
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=sqlite:///empty.db\nOPENAI_API_KEY=keep-private\n")
    return source, env, connection


def test_copy_keeps_wal_data_and_isolates_documents_and_configuration(tmp_path):
    source, env, source_db = source_runtime(tmp_path)
    previous_env = env.read_text()
    destination = tmp_path / "development"
    try:
        report = clone_runtime(source, destination, env)
        with sqlite3.connect(report["database"]) as copied:
            assert copied.execute("SELECT title FROM jobs").fetchone()[0] == "offer preserved"
            assert copied.execute("SELECT result FROM job_analyses").fetchone()[0] == "analysis preserved"
            path = copied.execute("SELECT path FROM generated_documents").fetchone()[0]
            assert path == str(destination / "documents" / "cv.pdf")
            assert copied.execute("SELECT cv_pdf_path FROM applications").fetchone()[0] == path
            Path(path).write_bytes(b"development PDF")
        assert (source / "documents" / "cv.pdf").read_bytes() == b"original PDF"
        assert source_db.execute("SELECT path FROM generated_documents").fetchone()[0] == str(
            source / "documents" / "cv.pdf"
        )
        assert (destination / "profile" / "profile.json").read_bytes() == (
            source / "profile" / "profile.json"
        ).read_bytes()
        assert (destination / ".env.before-copy").read_text() == previous_env
        settings = dotenv_values(env)
        assert settings["OPENAI_API_KEY"] == "keep-private"
        assert settings["DATABASE_URL"] == f"sqlite:///{destination / 'data' / 'smartapply.db'}"
        assert settings["OUTPUT_DIR"] == str(destination / "documents")
        with pytest.raises(FileExistsError):
            clone_runtime(source, destination, env)
        assert source_db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    finally:
        source_db.close()


def test_copy_rejects_production_destination_and_configuration(tmp_path):
    source, env, source_db = source_runtime(tmp_path)
    previous_env = env.read_text()
    try:
        with pytest.raises(ValueError):
            clone_runtime(source, source / "development", env)
        with pytest.raises(ValueError):
            clone_runtime(source, tmp_path / "development", source / ".env")
        assert env.read_text() == previous_env
        assert not (source / "development").exists()
    finally:
        source_db.close()
