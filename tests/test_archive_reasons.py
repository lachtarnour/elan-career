"""Tests for archive audit separation and product-facing labels."""

from smartapply.filtering.hard_reject.role_families import (
    TITLE_FAMILIES,
    TitleFamilyPolicy,
)
from smartapply.jobsearch.archive_reasons import (
    archive_reason_labels,
    decisive_rejection_reasons,
)
from smartapply.pipeline.process.audit import _rejection_audit_components


def test_diagnostics_are_not_persisted_as_rejection_causes() -> None:
    reasons = [
        "search_context:origin=personalized_matches,chips=pages=150",
        "remote_structured:hybrid",
        "offer_language:fr",
        "experience_structured_welcometothejungle:5",
        "experience_required_too_high:5+ years",
    ]

    audit = _rejection_audit_components("local_filter", reasons)

    assert audit["reasons"] == reasons
    assert audit["rejection_reasons"] == ["experience_required_too_high:5+ years"]
    assert "search_context" not in str(audit["rejection_summary"])


def test_duplicate_label_prioritizes_the_offer_reference() -> None:
    labels = archive_reason_labels(
        ["duplicate_of:42", "duplicate_reference:Acme — Data Scientist"],
        stage="deduplication",
        archived=True,
    )

    assert labels == ("Doublon de l’offre « Acme — Data Scientist »",)


def test_decisive_reason_filter_keeps_business_signals() -> None:
    assert decisive_rejection_reasons(
        ["role_relevance_score:0.08", "pure_data_engineering_role"]
    ) == ["pure_data_engineering_role"]


def test_every_hard_role_family_has_a_human_readable_archive_label() -> None:
    hard_families = {
        family.name
        for family in TITLE_FAMILIES
        if family.policy is TitleFamilyPolicy.HARD_REJECT
    }

    labels = {
        family: archive_reason_labels(
            [f"off_target_role_family:{family}"],
            archived=True,
        )[0]
        for family in hard_families
    }

    assert all(not label.startswith("Motif de filtrage") for label in labels.values())


def test_mandatory_doctorate_has_a_human_readable_archive_label() -> None:
    assert archive_reason_labels(
        ["qualification_required:doctorate"],
        archived=True,
    ) == ("Doctorat obligatoire non compatible avec le profil actuel",)
