"""Source credential refresh must not reconfigure the active runtime."""

from __future__ import annotations

import pytest

from smartapply import config
from smartapply.desktop.services import DesktopService
from smartapply.desktop.source_health import pending_source_health
from smartapply.scrapers import get_scraper


@pytest.fixture
def source_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(path))
    for field in config.Settings.model_fields:
        if field == "apify_token" or field.startswith(
            ("serpapi_", "francetravail_", "linkedin_", "wttj_")
        ):
            monkeypatch.delenv(field.upper(), raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("Source refresh tests must never call an API")

    monkeypatch.setattr("requests.sessions.Session.request", no_network)
    config.get_settings.cache_clear()
    yield path
    config.get_settings.cache_clear()


def test_source_reload_updates_credentials_and_options_without_mutating_runtime(source_env):
    source_env.write_text("APIFY_TOKEN=old-token\nLINKEDIN_MAX_RESULTS=5\n", encoding="utf-8")
    original = config.get_settings()
    original_data = original.model_dump()
    source_env.write_text(
        "APIFY_TOKEN=new-token\nLINKEDIN_MAX_RESULTS=80\n"
        "LINKEDIN_MAX_CONCURRENT_RUNS=4\nLINKEDIN_HTTP_TIMEOUT=20\n"
        "DATABASE_URL=sqlite:///must-not-switch.db\nPROFILE_DIR=/must-not-switch\n"
        "OPENAI_MODEL_SMART=must-not-switch\nOPENAI_MAX_COMPLETION_TOKENS=invalid\n",
        encoding="utf-8",
    )

    fresh = config.get_source_settings()

    assert fresh is not original
    assert fresh.apify_token == "new-token"
    assert fresh.linkedin_max_results == 80
    assert fresh.linkedin_max_concurrent_runs == 4
    assert get_scraper("linkedin").max_concurrent_runs == 3
    assert fresh.linkedin_http_timeout == 20
    for field in (
        "database_url",
        "profile_dir",
        "output_dir",
        "cache_dir",
        "openai_model_smart",
        "openai_max_completion_tokens",
    ):
        assert getattr(fresh, field) == original_data[field]
    assert config.get_settings() is original
    assert original.model_dump() == original_data


def test_removed_credentials_are_not_kept_from_cache(source_env):
    source_env.write_text("APIFY_TOKEN=old-token\n", encoding="utf-8")
    original = config.get_settings()
    source_env.write_text("", encoding="utf-8")

    assert config.get_source_settings().apify_token == ""
    assert original.apify_token == "old-token"
    assert not get_scraper("linkedin").is_available()


def test_process_environment_keeps_precedence_over_dotenv(source_env, monkeypatch):
    source_env.write_text("APIFY_TOKEN=file-token\n", encoding="utf-8")
    config.get_settings()
    monkeypatch.setenv("APIFY_TOKEN", "process-token")

    assert config.get_source_settings().apify_token == "process-token"
    assert get_scraper("linkedin").token == "process-token"


@pytest.mark.parametrize(
    ("source", "env_key", "attribute", "extra"),
    [
        ("linkedin", "APIFY_TOKEN", "token", ""),
        ("serpapi", "SERPAPI_API_KEY", "api_key", ""),
        (
            "francetravail",
            "FRANCETRAVAIL_CLIENT_ID",
            "client_id",
            "FRANCETRAVAIL_CLIENT_SECRET=test-secret\n",
        ),
        ("welcometothejungle", "WTTJ_COOKIE", "cookie_header", ""),
    ],
)
def test_diagnostics_and_new_scrapers_use_rotated_credentials(
    source_env,
    monkeypatch,
    source,
    env_key,
    attribute,
    extra,
):
    source_env.write_text(f"{env_key}=old-value\n{extra}", encoding="utf-8")
    config.get_settings()
    previous_scraper = get_scraper(source)
    checked = []

    def capture_health(settings):
        checked.append(getattr(settings, env_key.lower()))
        return pending_source_health(settings)

    monkeypatch.setattr("smartapply.desktop.services.check_source_health", capture_health)
    source_env.write_text(f"{env_key}=new-value\n{extra}", encoding="utf-8")

    DesktopService().diagnostics(check_sources=True)
    current_scraper = get_scraper(source)

    assert checked == ["new-value"]
    assert getattr(current_scraper, attribute) == "new-value"
    assert getattr(previous_scraper, attribute) == "old-value"


def test_desktop_search_uses_refreshed_linkedin_limit_and_token(source_env, monkeypatch):
    from smartapply.pipeline.ingest import IngestCollection, IngestReport
    from smartapply.pipeline.ingestor import Ingestor
    from smartapply.pipeline.pipeline import Pipeline

    source_env.write_text("APIFY_TOKEN=old-token\nLINKEDIN_MAX_RESULTS=5\n", encoding="utf-8")
    config.get_settings()
    source_env.write_text("APIFY_TOKEN=new-token\nLINKEDIN_MAX_RESULTS=25\n", encoding="utf-8")
    collected = []

    def collect(self, source, query, location, **kwargs):
        collected.append((get_scraper(source).token, kwargs["max_results"]))
        return IngestCollection(source=source, raw_jobs=[])

    monkeypatch.setattr(Ingestor, "collect_source", collect)
    monkeypatch.setattr(
        Pipeline,
        "persist_collection",
        lambda self, value: IngestReport(source=value.source, fetched=0, persisted=0),
    )

    result = DesktopService().search_jobs(
        query="Data Scientist",
        location="Paris",
        sources=["linkedin"],
        max_results=20,
        date_posted="week",
    )

    assert collected == [("new-token", 20)]
    assert result.errors == []
