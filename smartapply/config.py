"""Configuration centralisee chargee depuis .env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

DEFAULT_RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "Elan"
RUNTIME_DIR = Path(os.environ.get("ELAN_HOME", DEFAULT_RUNTIME_DIR)).expanduser().resolve()
ENV_FILE = Path(os.environ.get("ELAN_ENV_FILE", RUNTIME_DIR / ".env")).expanduser().resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LLM
    llm_provider: str = Field(default="openai")
    openai_api_key: str = Field(default="")
    openai_model_cheap: str = Field(default="gpt-5.4-mini")
    openai_model_smart: str = Field(default="gpt-5.4")
    openai_model_embed: str = Field(default="text-embedding-3-small")
    openai_max_completion_tokens: int = Field(default=6000, ge=256, le=100_000)
    llm_cache_ttl_days: int = Field(default=15, ge=1, le=365)
    # Embeddings
    embeddings_provider: str = Field(default="openai")
    local_embeddings_model: str = Field(default="paraphrase-multilingual-MiniLM-L12-v2")

    # Database
    database_url: str = Field(default=f"sqlite:///{RUNTIME_DIR / 'data' / 'smartapply.db'}")

    # Paths
    profile_dir: Path = Field(default=RUNTIME_DIR / "profile")
    output_dir: Path = Field(default=RUNTIME_DIR / "documents")
    cache_dir: Path = Field(default=RUNTIME_DIR / "cache")

    # SerpApi Google Jobs
    serpapi_api_key: str = Field(default="")
    serpapi_google_domain: str = Field(default="google.com")
    # Comma-separated Google Jobs UI languages. "en,fr" searches both English
    # and French result contexts while keeping the country/location in France.
    serpapi_hl: str = Field(default="en,fr")
    serpapi_gl: str = Field(default="fr")
    serpapi_default_location: str = Field(default="Paris, France")
    serpapi_max_pages: int = Field(default=3, ge=1, le=300)
    # Low-result fallback target. Default 10 fills one Google Jobs page after
    # strict chips underperform; raise to 20/30 with SERPAPI_MAX_PAGES >= 2/3.
    serpapi_low_result_fallback_target: int = Field(default=10, ge=0, le=300)
    # Google Jobs freshness filter. Allowed values: any, today, 3days, week, month.
    # "week" maps to the Google Jobs "Last week" / last 7 days filter.
    serpapi_date_posted: str = Field(default="week")
    # Optional raw Google Jobs filter string from SerpApi's `filters[].parameters.uds`.
    serpapi_uds: str = Field(default="")

    # France Travail
    francetravail_client_id: str = Field(default="")
    francetravail_client_secret: str = Field(default="")
    francetravail_scope: str = Field(default="api_offresdemploiv2 o2dsoffre")
    francetravail_timeout: int = Field(default=30, ge=1, le=120)

    # LinkedIn via Apify valig/linkedin-jobs-scraper
    apify_token: str = Field(default="")
    linkedin_contract_type: str = Field(default="F")
    linkedin_experience_level: str = Field(default="2,3,4")
    linkedin_remote: str = Field(default="1,2,3")
    linkedin_date_posted: str = Field(default="week")
    linkedin_timeout: int = Field(default=180, ge=1, le=300)
    linkedin_max_results: int = Field(default=50, ge=1, le=300)

    # Welcome to the Jungle personalized matches
    wttj_cookie: str = Field(default="")
    wttj_max_pages: int = Field(default=150, ge=1, le=500)
    wttj_pages: int = Field(default=150, ge=1, le=500)
    wttj_per_page: int = Field(default=50, ge=1, le=100)
    wttj_include_company_profile: bool = Field(default=True)
    wttj_skip_failed_jobs: bool = Field(default=True)
    wttj_timeout: int = Field(default=30, ge=1, le=120)
    wttj_delay_seconds: float = Field(default=0.5, ge=0.0, le=10.0)
    wttj_analyzer_metadata_fields: str = Field(
        default=(
            "company_website,company_domain,company_profile_url,sectors,offices,"
            "company_stats,company_summary,skills,workplace,"
            "remote,contract_type,experience_level,salary,published_at,profession,"
            "apply_url"
        )
    )

    # Pipeline tuning
    top_k_ranked: int = Field(default=25, ge=1)
    dedup_title_threshold: int = Field(default=85, ge=0, le=100)
    dedup_desc_threshold: int = Field(default=70, ge=0, le=100)
    # Concurrency for parallel LLM analysis calls in Processor. Capped to
    # respect provider rate limits while still giving a large wall-clock win
    # on top-K analysis (15 jobs go from ~30s serial to ~4s with 5 workers).
    llm_max_concurrent: int = Field(default=5, ge=1, le=20)

    # CV skill-block presentation. Disabling this restores the previous
    # renderer behaviour without changing the stored CV adaptation.
    cv_merge_sparse_secondary_skills: bool = Field(default=True)
    cv_secondary_skill_block_min_size: int = Field(default=4, ge=2, le=12)

    # Logging
    log_level: str = Field(default="INFO")

    def ensure_dirs(self) -> None:
        for p in (self.output_dir, self.cache_dir):
            p.mkdir(parents=True, exist_ok=True)

        # SQLite does not create parent directories when opening a database.
        # Ensure a fresh installation can initialize its database before the
        # first connection is opened.
        database_url = make_url(self.database_url)
        if database_url.get_backend_name() == "sqlite" and database_url.database not in {
            None,
            ":memory:",
        }:
            Path(database_url.database).expanduser().resolve().parent.mkdir(
                parents=True,
                exist_ok=True,
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def get_source_settings() -> Settings:
    """Reload source configuration without changing the active application runtime.

    Credentials and source options follow the normal environment-over-dotenv
    precedence on every call. All other settings remain pinned to the cached
    runtime, so refreshing a source cannot switch a database, profile or model
    underneath an ongoing operation. Existing settings/scrapers are untouched.
    """
    source_fields = {
        name
        for name in Settings.model_fields
        if name == "apify_token"
        or name.startswith(("serpapi_", "francetravail_", "linkedin_", "wttj_"))
    }
    runtime = get_settings().model_dump(exclude=source_fields)
    return Settings(_env_file=str(ENV_FILE), **runtime)
