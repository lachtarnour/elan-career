"""Processing phase mixins."""

from smartapply.pipeline.process.analysis import AnalysisMixin
from smartapply.pipeline.process.audit import (
    _is_anonymous_company,
    _rejection_audit_components,
    _should_replace_job_location,
)
from smartapply.pipeline.process.local_filtering import LocalFilterMixin
from smartapply.pipeline.process.ranking import ShortlistMixin

__all__ = [
    "AnalysisMixin",
    "LocalFilterMixin",
    "ShortlistMixin",
    "_is_anonymous_company",
    "_rejection_audit_components",
    "_should_replace_job_location",
]
