"""Configuration pytest commune."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Forcer un environnement de test reproductible avant l'import des modules.
# On ECRASE (pas setdefault) les cles sensibles pour que les tests soient
# independants du .env local et n'appellent jamais les vraies API.
_runtime = tempfile.TemporaryDirectory(prefix="elan-tests-")
_runtime_path = Path(_runtime.name)
os.environ.update(
    ELAN_HOME=str(_runtime_path),
    ELAN_ENV_FILE=str(_runtime_path / ".env"),
    DATABASE_URL=f"sqlite:///{_runtime_path / 'test.db'}",
    OUTPUT_DIR=str(_runtime_path / "documents"),
    CACHE_DIR=str(_runtime_path / "cache"),
    PROFILE_DIR=str(Path(__file__).parent / "fixtures" / "profile"),
    LLM_PROVIDER="mock",
    EMBEDDINGS_PROVIDER="mock",
    OPENAI_API_KEY="",
    SERPAPI_API_KEY="",
    FRANCETRAVAIL_CLIENT_ID="",
    FRANCETRAVAIL_CLIENT_SECRET="",
    APIFY_TOKEN="",
    WTTJ_COOKIE="",
)

# Clear any cached settings before tests
from smartapply.config import get_settings as _gs  # noqa: E402

_gs.cache_clear()


# ============================================================
# Auto-fixtures used across the whole suite
# ============================================================


@pytest.fixture(autouse=True)
def _fast_pdf(request, monkeypatch):
    """Stub ``html_to_pdf`` by default so the suite stays fast.

    Tests that need a real PDF (visual rendering, page-count logic) opt out
    with ``@pytest.mark.real_pdf``. This cuts ~70s off the suite on machines
    where Chrome headless is the only renderer available.
    """
    if "real_pdf" in request.keywords:
        return

    def _fake_pdf(html: str, pdf_path, *, base_dir=None):  # noqa: ARG001
        path = Path(pdf_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n%%fake-test-fixture\n")
        return path

    monkeypatch.setattr("smartapply.cv.html_renderer.html_to_pdf", _fake_pdf)


@pytest.fixture(autouse=True)
def _isolate_default_apify_recovery_journal():
    """Prevent direct scraper tests from sharing durable run acknowledgements."""
    journal_path = _gs().cache_dir / "apify_pending_runs.json"
    journal_path.unlink(missing_ok=True)
    yield
    journal_path.unlink(missing_ok=True)


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Per-test SQLite DB + output dir, with cached settings/engine reset.

    Used by every module-level integration test. Keeping it here removes the
    same fixture from 4+ test files.
    """
    db_path = tmp_path / "test.db"
    output_dir = tmp_path / "output"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))

    from smartapply.config import get_settings

    get_settings.cache_clear()
    from smartapply.database.session import reset_engine_cache

    reset_engine_cache()
    from smartapply.database.session import init_db

    init_db()
    yield tmp_path
    reset_engine_cache()
    get_settings.cache_clear()


# ============================================================
# Lightweight fixtures
# ============================================================


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    (tmp_path / "output").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "samples").mkdir()
    return tmp_path


@pytest.fixture
def sample_job_dict() -> dict:
    return {
        "title": "Data Scientist NLP",
        "company": "Acme AI",
        "location": "Paris, France",
        "contract_type": "CDI",
        "remote_policy": "hybrid",
        "description": (
            "Nous recherchons un Data Scientist NLP pour developper des pipelines RAG "
            "et fine-tuner des modeles de langue. Stack: Python, PyTorch, Hugging Face. "
            "3+ ans d'experience. Avantages: tickets restaurant, mutuelle, RTT."
        ),
        "application_url": "https://acme.example.com/jobs/42",
        "source": "manual",
    }
