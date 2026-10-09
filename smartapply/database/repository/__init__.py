"""High-level repository helpers grouped by aggregate."""

from smartapply.database.repository.analyses import set_analysis
from smartapply.database.repository.applications import (
    clear_shortlist_for_sent_applications,
    create_or_get_application,
    list_applications,
    update_application_tracking,
)
from smartapply.database.repository.documents import add_document, upsert_document
from smartapply.database.repository.duplicates import (
    application_for_duplicate_group,
    application_ids_for_confirmed_groups,
    canonical_job,
    confirm_duplicate,
    duplicate_group_ids,
    pending_duplicate_for_group,
    pending_duplicate_job,
    reject_duplicate,
)
from smartapply.database.repository.jobs import (
    get_job_by_external_id,
    get_known_external_ids,
    list_jobs,
    list_known_jobs,
    list_pending_processing,
    mark_analyzed,
    mark_archived,
    mark_filtered,
    rescue_archived_job,
    set_shortlisted,
    update_status,
    upsert_job,
)
from smartapply.database.repository.llm_cache import (
    cache_get,
    cache_set,
    purge_expired_cache,
    record_usage,
    total_cost,
)
from smartapply.database.repository.scores import set_score

__all__ = [
    "add_document",
    "application_for_duplicate_group",
    "application_ids_for_confirmed_groups",
    "cache_get",
    "cache_set",
    "create_or_get_application",
    "canonical_job",
    "confirm_duplicate",
    "clear_shortlist_for_sent_applications",
    "get_job_by_external_id",
    "get_known_external_ids",
    "duplicate_group_ids",
    "list_applications",
    "list_jobs",
    "list_known_jobs",
    "list_pending_processing",
    "mark_analyzed",
    "mark_archived",
    "mark_filtered",
    "purge_expired_cache",
    "pending_duplicate_job",
    "pending_duplicate_for_group",
    "record_usage",
    "reject_duplicate",
    "rescue_archived_job",
    "set_shortlisted",
    "set_analysis",
    "set_score",
    "total_cost",
    "update_application_tracking",
    "update_status",
    "upsert_document",
    "upsert_job",
]
