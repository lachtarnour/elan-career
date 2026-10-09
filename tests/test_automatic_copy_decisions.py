"""Pure automatic-copy planning: real reduction without losing a user's work."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from smartapply.dedup.automatic_copies import copy_alias_can_be_archived
from smartapply.dedup.review_decisions import (
    automatic_match_reason,
    plan_pending_review_decisions,
)

AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
DESCRIPTION = (
    "Build production ML pipelines using Python, PyTorch and SQL. Evaluate "
    "models, monitor deployment, document results and collaborate with product "
    "teams. This full original description includes requirement number 42. "
) * 3


def _job(job_id, **updates):
    values = {
        "id": job_id, "title": "Data Scientist", "company": "Example Corp",
        "location": "Paris", "contract_type": "CDI", "remote_policy": "hybrid",
        "description": DESCRIPTION, "cleaned_description": DESCRIPTION,
        "source": "linkedin", "source_data": None,
        "external_id": f"linkedin:{job_id}",
        "application_url": f"https://linkedin.com/jobs/view/{job_id}",
        "published_date": AT, "scraped_at": AT,
        "canonical_job_id": None, "possible_duplicate_of_id": None,
        "duplicate_review_status": "none", "duplicate_match_type": None,
        "duplicate_confidence": None, "status": "scraped", "archived_at": None,
        "application": None, "analysis": None, "score": None,
        "filtered_at": None, "ranked_at": None, "analyzed_at": None,
        "shortlisted_at": None, "shortlist_origin": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _pending(job_id, reference_id, **updates):
    return _job(job_id, possible_duplicate_of_id=reference_id,
                duplicate_review_status="pending", duplicate_match_type="fuzzy",
                duplicate_confidence=0.99, **updates)


def _confirms(decisions):
    return [decision for decision in decisions if decision.action == "confirm"]


def test_scored_filtered_copy_is_really_confirmed_without_mutating_inputs():
    score = SimpleNamespace(components={"filter_disposition": "pending_relevance"})
    reference = _job(1, application=SimpleNamespace(id=17), status="sent")
    alias = _pending(2, 1, score=score, filtered_at=AT, ranked_at=AT, status="filtered")
    before = [vars(job).copy() for job in (reference, alias)]

    decisions = plan_pending_review_decisions([reference, alias])

    assert [(item.job_id, item.after["canonical_job_id"], item.reason)
            for item in _confirms(decisions)] == [(2, 1, "strict_content_copy")]
    assert decisions[0].after["status"] == "archived"
    assert [vars(job) for job in (reference, alias)] == before
    assert alias.score is score


def test_newest_active_copy_becomes_keeper_with_audited_root_replacement():
    older = _job(1, score=SimpleNamespace(components={}), filtered_at=AT)
    newer = _pending(2, 1, published_date=AT + timedelta(days=1))

    decisions = plan_pending_review_decisions([older, newer])

    assert [(item.job_id, item.action, item.reason) for item in decisions] == [
        (2, "release", "strict_copy_canonical_keeper"),
        (1, "confirm", "strict_content_copy"),
    ]
    assert decisions[-1].after["canonical_job_id"] == 2
    assert decisions[-1].reference_id == 2
    assert newer.possible_duplicate_of_id == 1  # pure plan


@pytest.mark.parametrize("updates", [
    {"source": "manual"}, {"application": SimpleNamespace(id=1)},
    {"analysis": SimpleNamespace(id=1)}, {"analyzed_at": AT},
    {"shortlisted_at": AT}, {"shortlist_origin": "automatic"},
    {"score": SimpleNamespace(components={"manual_keep": True})},
    {"score": SimpleNamespace(components={"manual_reject": False})},
    {"score": SimpleNamespace(components={"manual_rescue": {}})},
    {"score": SimpleNamespace(components={"reactivation": {}})},
    {"score": SimpleNamespace(components={"reasons": ["Manual override: kept by user"]})},
    {"score": SimpleNamespace(components={"rejection_stage": "manual"})},
    {"duplicate_review_status": "rejected"},
    {"status": "archived", "archived_at": AT},
])
def test_manual_or_workflow_rows_cannot_be_archived(updates):
    assert not copy_alias_can_be_archived(_job(1, **updates))


def test_two_dossiers_are_never_automatically_merged():
    reference = _job(1, application=SimpleNamespace(id=1))
    alias = _pending(2, 1, application=SimpleNamespace(id=2))
    assert not _confirms(plan_pending_review_decisions([reference, alias]))


def test_archived_automatic_filter_copy_keeps_its_archive_timestamp():
    score = SimpleNamespace(components={
        "filter_disposition": "rejected", "rejection_stage": "local_filter",
        "rejection_reasons": ["contract"],
    })
    reference = _job(1, application=SimpleNamespace(id=1))
    alias = _pending(2, 1, status="archived", archived_at=AT, score=score)
    confirm = _confirms(plan_pending_review_decisions([reference, alias]))[0]
    assert confirm.after["archived_at"] == AT


def test_active_copy_is_not_archived_into_an_archived_manual_reference():
    reference = _job(1, status="archived", archived_at=AT)
    alias = _pending(2, 1)
    assert automatic_match_reason(alias, reference, [reference, alias]) is None
    assert not _confirms(plan_pending_review_decisions([reference, alias]))


def test_active_copy_replaces_an_automatically_rejected_archived_reference():
    reference = _job(1, status="archived", archived_at=AT, score=SimpleNamespace(components={
        "filter_disposition": "rejected", "rejection_stage": "local_filter",
        "rejection_reasons": ["contract"],
    }))
    alias = _pending(2, 1)
    confirms = _confirms(plan_pending_review_decisions([reference, alias]))
    assert [(item.job_id, item.after["canonical_job_id"]) for item in confirms] == [(1, 2)]


def test_pending_cycle_chooses_newest_copy_deterministically():
    first = _pending(1, 2)
    second = _pending(2, 1, published_date=AT + timedelta(days=1))
    forward = plan_pending_review_decisions([first, second])
    reverse = plan_pending_review_decisions([second, first])
    def summary(decisions):
        return [(item.job_id, item.action, item.after["canonical_job_id"]) for item in decisions]
    assert summary(forward) == summary(reverse) == [(2, "release", None), (1, "confirm", 2)]


def test_confirmed_chain_resolves_to_an_existing_protected_canonical_root():
    root = _job(1, application=SimpleNamespace(id=1))
    confirmed = _job(2, canonical_job_id=1, duplicate_review_status="confirmed",
                     status="archived", archived_at=AT)
    alias = _pending(3, 2)
    confirms = _confirms(plan_pending_review_decisions([alias, confirmed, root]))
    assert [(item.job_id, item.after["canonical_job_id"]) for item in confirms] == [(3, 1)]
    assert confirms[0].reference_id == 1


def test_pending_root_retains_its_unproven_upstream_review():
    upstream = _job(1, description=DESCRIPTION + " Different team.")
    keeper = _pending(2, 1, application=SimpleNamespace(id=1))
    alias = _pending(3, 2)
    confirms = _confirms(plan_pending_review_decisions([upstream, keeper, alias]))
    assert [(item.job_id, item.after["canonical_job_id"]) for item in confirms] == [(3, 2)]
    assert keeper.possible_duplicate_of_id == 1


def test_newer_child_does_not_erase_an_automatic_roots_unproven_upstream_review():
    upstream = _job(1, description=DESCRIPTION + " Different team.")
    keeper = _pending(2, 1)
    newer = _pending(3, 2, published_date=AT + timedelta(days=1))
    decisions = plan_pending_review_decisions([upstream, keeper, newer])
    confirms = _confirms(decisions)
    assert [(item.job_id, item.after["canonical_job_id"]) for item in confirms] == [(3, 2)]
    assert not any(item.job_id == 2 for item in decisions)


def test_distinct_offer_decision_on_reference_is_kept():
    reference = _job(1, duplicate_review_status="rejected")
    newer = _pending(2, 1, published_date=AT + timedelta(days=1))
    decisions = plan_pending_review_decisions([reference, newer])
    assert [(item.job_id, item.after["canonical_job_id"])
            for item in _confirms(decisions)] == [(2, 1)]
    assert not any(item.job_id == 1 for item in decisions)


def test_unrelated_pending_copy_does_not_join_another_copy_edge():
    upstream = _job(1, description=DESCRIPTION + " Different team.")
    unrelated = _pending(2, 1)
    reference = _job(3)
    alias = _pending(4, 3)
    decisions = plan_pending_review_decisions([upstream, unrelated, reference, alias])
    assert not any(item.job_id == 2 for item in decisions)
    assert len(_confirms(decisions)) == 1


@pytest.mark.parametrize("updates", [
    {"title": "Graduate Data Scientist"}, {"contract_type": "CDD"},
    {"remote_policy": "onsite"}, {"company": "Different Corp"},
])
def test_upstream_literal_metadata_conflict_blocks_automatic_content_confirmation(updates):
    reference = _job(1)
    alias = _pending(2, 1)
    sibling = _pending(3, 1, **updates)
    assert automatic_match_reason(alias, reference, [reference, alias, sibling]) is None


def test_complete_group_checks_all_dates_and_does_not_bridge_thirty_days():
    reference = _job(1, published_date=AT + timedelta(days=20))
    alias = _pending(2, 1, published_date=AT + timedelta(days=40))
    earlier = _pending(3, 1, published_date=AT)
    assert automatic_match_reason(alias, reference, [reference, alias, earlier]) is None


@pytest.mark.parametrize("updates", [
    {"description": DESCRIPTION + " Additional requirement."},
    {"description": "Short description"}, {"location": "France"},
    {"source_data": {"employer_reference": "REQ-DIFFERENT"}},
])
def test_incomplete_or_nonidentical_copy_stays_for_review(updates):
    reference = _job(1)
    alias = _pending(2, 1, **updates)
    assert automatic_match_reason(alias, reference, [reference, alias]) is None


def test_source_identity_rule_still_accepts_its_original_short_description_case():
    reference = _job(1, description="Short original text", external_id="linkedin:4464028174",
                     application_url="https://linkedin.com/jobs/view/4464028174")
    alias = _pending(2, 1, description="Short original text", external_id=reference.external_id,
                     application_url=reference.application_url)
    assert automatic_match_reason(alias, reference, [reference, alias]) == "source_identity"


def test_source_identity_cannot_join_an_existing_group_with_a_forty_day_span():
    url = "https://linkedin.com/jobs/view/4464028174"
    reference = _job(1, source="serpapi", external_id="serpapi:one", application_url=url,
                     description="Short source text", published_date=AT)
    middle = _job(2, canonical_job_id=1, duplicate_review_status="confirmed",
                  published_date=AT + timedelta(days=20))
    later = _job(3, canonical_job_id=1, duplicate_review_status="confirmed",
                 published_date=AT + timedelta(days=40))
    target = _job(4, source="serpapi", external_id="serpapi:new", application_url=url,
                  description="Short source text", published_date=AT + timedelta(days=20))
    assert automatic_match_reason(target, reference, [reference, middle, later]) is None


@pytest.mark.parametrize("field,root_value,sibling_value,unknown", [
    ("location", "Paris", "Lyon", "France"),
    ("contract_type", "CDI", "CDD", None),
])
def test_source_identity_cannot_bridge_sibling_contradictions_through_unknown_metadata(
    field, root_value, sibling_value, unknown,
):
    url = "https://linkedin.com/jobs/view/4464028174"
    reference = _job(1, source="serpapi", external_id="serpapi:one", application_url=url,
                     description="Short source text", **{field: root_value})
    sibling = _job(2, canonical_job_id=1, duplicate_review_status="confirmed",
                   **{field: sibling_value})
    target = _job(3, source="serpapi", external_id="serpapi:new", application_url=url,
                  description="Short source text", **{field: unknown})
    assert automatic_match_reason(target, reference, [reference, sibling]) is None


def test_fresh_raw_copy_has_no_workflow_status_and_cannot_hide_into_archived_reference():
    reference = _job(1, status="archived", archived_at=AT)
    raw = _job(2)
    del raw.status
    del raw.archived_at
    del raw.scraped_at
    assert automatic_match_reason(raw, reference, [reference]) is None
    reference.status = "scraped"
    reference.archived_at = None
    assert automatic_match_reason(raw, reference, [reference]) == "strict_content_copy"


def test_content_evidence_stays_separate_from_similarity():
    reference = _job(1)
    alias = _pending(2, 1, description=DESCRIPTION.replace("42", "43"))
    assert automatic_match_reason(alias, reference, [reference, alias]) is None
