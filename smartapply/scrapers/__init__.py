"""Scrapers — collect jobs from multiple sources via a common interface."""

from smartapply.scrapers.base import (
    Scraper,
    ScraperConfigError,
    ScraperError,
    ScraperStopError,
)
from smartapply.scrapers.francetravail import FranceTravailScraper
from smartapply.scrapers.linkedin import LinkedInJobsScraper
from smartapply.scrapers.manual import ManualScraper
from smartapply.scrapers.registry import (
    available_scrapers,
    get_scraper,
)
from smartapply.scrapers.serpapi import (
    SERPAPI_DATE_POSTED_LABELS,
    SERPAPI_DATE_POSTED_OPTIONS,
    SerpApiGoogleJobsScraper,
)
from smartapply.scrapers.welcometothejungle import WelcomeToTheJungleScraper

__all__ = [
    "FranceTravailScraper",
    "LinkedInJobsScraper",
    "ManualScraper",
    "Scraper",
    "ScraperConfigError",
    "ScraperError",
    "ScraperStopError",
    "SERPAPI_DATE_POSTED_LABELS",
    "SERPAPI_DATE_POSTED_OPTIONS",
    "SerpApiGoogleJobsScraper",
    "WelcomeToTheJungleScraper",
    "available_scrapers",
    "get_scraper",
]
