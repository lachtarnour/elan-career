"""Common types and abstract base class for job scrapers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from smartapply.offers import RawJob


class Scraper(ABC):
    """Abstract base class for searchable job sources."""

    name: str = ""

    @abstractmethod
    def search(
        self,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = None,
        **kwargs: Any,
    ) -> Iterator[RawJob]:
        """Yield jobs matching the search query."""

    def is_available(self) -> bool:
        """Return True if the scraper has the credentials/config it needs."""
        return True


class ScraperError(RuntimeError):
    """Base exception for scrapers."""


class ScraperStopError(ScraperError):
    """A source error after which no additional remote requests are safe."""


class ScraperConfigError(ScraperStopError):
    """Raised when a scraper is misconfigured (missing API key, etc.)."""


__all__ = [
    "Scraper",
    "ScraperConfigError",
    "ScraperError",
    "ScraperStopError",
]
