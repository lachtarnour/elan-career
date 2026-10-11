"""End-to-end pipeline orchestration.

Internally, ``Pipeline`` is a thin facade composing focused phase modules
(``ingestor``, ``processor``, ``applier`` and ``application_renderer``).
"""

from smartapply.language import detect_offer_language
from smartapply.pipeline.application_renderer import ApplicationDocumentRenderer
from smartapply.pipeline.applier import Applier
from smartapply.pipeline.errors import ApplicationAlreadyExistsError, DuplicateReviewRequiredError
from smartapply.pipeline.ingestor import Ingestor, IngestReport
from smartapply.pipeline.pipeline import Pipeline
from smartapply.pipeline.processor import Processor
from smartapply.pipeline.reports import (
    AnalyzeReport,
    ApplyReport,
    LocalFilterReport,
    ProcessReport,
)

__all__ = [
    "Applier",
    "AnalyzeReport",
    "ApplicationDocumentRenderer",
    "ApplicationAlreadyExistsError",
    "DuplicateReviewRequiredError",
    "ApplyReport",
    "IngestReport",
    "Ingestor",
    "Pipeline",
    "LocalFilterReport",
    "ProcessReport",
    "Processor",
    "detect_offer_language",
]
