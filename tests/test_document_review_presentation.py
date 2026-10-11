"""Product copy for stored CV and motivation-letter diagnostics."""

from __future__ import annotations

import pytest

from smartapply.cv.motivation_validator import MotivationLetterValidator
from smartapply.desktop.document_review_presentation import present_document_review


def _keys(messages: list[str]) -> list[str]:
    return [point["key"] for point in present_document_review(messages)["points"]]


@pytest.mark.parametrize(
    ("messages", "expected_keys", "expected_corrected"),
    [
        # The first screenshot includes a historical local shortening and a
        # current title-alignment warning.
        (
            ["letter_shortened_locally_then_reviewed", "cv_title_not_offer_anchored"],
            ["cv_title_alignment"],
            False,
        ),
        (
            [
                "text_review_corrected: correction vérifiée",
                "cv_title_not_offer_anchored",
            ],
            ["cv_title_alignment"],
            True,
        ),
        (
            [
                "letter_too_long:252",
                "summary_not_offer_anchored",
                "validation_error:text_review_structural_error:letter_too_long:252",
                "validation_error:letter_too_long:252",
            ],
            ["letter_length", "summary_alignment"],
            False,
        ),
        (
            [
                "text_review:structure:motivation_letter_body: The body exceeds the maximum "
                "length [passage='full private letter text'; sources=style:letter]",
                "letter_too_long:242",
                "summary_not_offer_anchored",
                "validation_error:text_review_invalid_correction:letter_too_long:241",
                "validation_error:letter_too_long:242",
                "validation_error:text_review_incomplete: contrôle non terminé",
            ],
            [
                "structure:motivation_letter_body",
                "letter_length",
                "summary_alignment",
                "review_status",
            ],
            False,
        ),
        (
            [
                "letter_too_long:270",
                "validation_error:text_review_structural_error:letter_too_long:270",
                "validation_error:letter_too_long:270",
            ],
            ["letter_length"],
            False,
        ),
    ],
)
def test_screenshot_diagnostics_are_deduplicated(
    messages: list[str], expected_keys: list[str], expected_corrected: bool
) -> None:
    result = present_document_review(messages)

    assert [point["key"] for point in result["points"]] == expected_keys
    assert result["corrected"] is expected_corrected
    assert all(point["label"] and point["detail"] for point in result["points"])


def test_final_document_word_count_wins_over_rejected_revision() -> None:
    messages = [
        "validation_error:text_review_invalid_correction:letter_too_long:241",
        "validation_error:text_review_structural_error:letter_too_long:242",
        "validation_error:letter_too_long:242",
        "letter_too_long:242",
    ]

    result = present_document_review(messages)

    assert _keys(messages) == ["letter_length", "review_status"]
    assert "242 mots" in result["points"][0]["detail"]
    assert "241" not in result["points"][0]["detail"]
    assert str(MotivationLetterValidator.__init__.__defaults__[1]) in result["points"][0]["detail"]


@pytest.mark.parametrize(
    "rejected",
    [
        "letter_too_long:241",
        "letter_too_short:178",
        "letter_not_3_paragraphs:4",
        "summary_too_long:len=560",
        "summary_too_many_lines:lines=6",
    ],
)
def test_rejected_revision_alone_never_describes_unpublished_text(rejected: str) -> None:
    result = present_document_review(
        [f"validation_error:text_review_invalid_correction:{rejected}"]
    )

    assert [point["key"] for point in result["points"]] == ["review_status"]
    assert result["points"][0]["label"] == "Correction proposée à revoir"
    assert not any(char.isdigit() for char in result["points"][0]["detail"])


def test_structural_error_alone_does_not_expose_candidate_word_count() -> None:
    result = present_document_review(
        ["validation_error:text_review_structural_error:letter_too_long:252"]
    )

    assert [point["key"] for point in result["points"]] == ["review_status"]
    assert "252" not in result["points"][0]["detail"]
    assert "structure" in result["points"][0]["detail"]


def test_nested_prefixes_do_not_hide_a_distinct_structure_finding() -> None:
    messages = [
        "validation_error:text_review_invalid_correction:text_review_structural_error:"
        "letter_too_long:241",
        "text_review:structure:motivation_letter_body: The subject is missing "
        "[passage='private letter text'; sources=style:letter]",
        "letter_too_long:242",
    ]

    keys = _keys(messages)
    assert "letter_length" in keys
    assert "structure:motivation_letter_body" in keys
    assert len(keys) >= 2


def test_unknown_and_exception_details_never_reach_product_copy() -> None:
    secret = "private passage, API credential, and raw exception" * 50
    messages = [
        "unexpected_diagnostic:" + secret,
        "pdf_generation_skipped:" + secret,
        "text_review:factual:professional_summary:" + secret,
    ]

    result = present_document_review(messages)
    copy = " ".join(
        part for point in result["points"] for part in (point["label"], point["detail"])
    )

    assert _keys(messages) == ["other", "pdf_generation", "factual:professional_summary"]
    assert secret not in copy
    assert "unexpected_diagnostic" not in copy
    assert "text_review" not in copy
    assert "pdf_generation_skipped" not in copy
    assert len(copy) < 500


def test_verified_correction_and_style_hint_remain_brief_and_separate() -> None:
    secret = "a private stylistic suggestion from the reviewer"
    result = present_document_review(
        [
            f"text_review:style:professional_summary: {secret}",
            "auto_fixed:summary",
            "text_review_corrected: correction vérifiée",
        ]
    )

    assert result["corrected"] is True
    visible_points = (*result["points"], *result.get("advisories", ()))
    assert len(visible_points) == 1
    assert "style" in visible_points[0]["key"]
    assert secret not in str(visible_points)
    assert len(visible_points[0]["label"] + visible_points[0]["detail"]) < 200


def test_multiple_factual_findings_in_one_field_keep_a_visible_cardinality() -> None:
    messages = [
        "text_review:factual:motivation_letter_body: first private claim",
        "validation_error:text_review:factual:motivation_letter_body: first private claim",
        "text_review:factual:motivation_letter_body: second private claim",
    ]

    points = present_document_review(messages)["points"]
    factual = [point for point in points if point["key"].startswith("factual:")]
    copy = " ".join(part for point in factual for part in (point["label"], point["detail"]))

    assert factual
    assert len(factual) == 2 or "2" in copy
    assert "first private claim" not in copy
    assert "second private claim" not in copy


def test_summary_character_and_line_limits_are_both_visible() -> None:
    points = present_document_review(
        ["summary_too_long:len=560", "summary_too_many_lines:lines=6"]
    )["points"]
    copy = " ".join(part for point in points for part in (point["label"], point["detail"]))

    assert "caractère" in copy
    assert "ligne" in copy
    assert "560" in copy
    assert "6" in copy


def test_distinct_known_validation_errors_keep_specific_points() -> None:
    messages = [
        "validation_error:empty_field:cv_title",
        "validation_error:unknown_project_id:project-42",
        "validation_error:experience_order_mismatch:expected complete profile order",
        "validation_error:unknown_certificate_id:certificate-42",
    ]

    points = present_document_review(messages)["points"]
    keys = [point["key"] for point in points]

    assert len(keys) == 4
    assert len(set(keys)) == 4
    assert "other" not in keys
    assert all(point["label"] and point["detail"] for point in points)


def test_distinct_cv_and_letter_checks_are_not_collapsed_by_field() -> None:
    points = present_document_review(
        [
            "foreign_script_in_letter:cjk",
            "french_elision_missing_apostrophe",
            "low_text_overlap:experience-1 score=20",
            "off_allowed_claims:experience-1 score=20",
            "unknown_skill_profile:profile-1",
            "unknown_selected_skill:unsupported-skill",
        ]
    )["points"]

    assert len(points) == len({point["key"] for point in points}) == 6
    assert all(point["label"] and point["detail"] for point in points)


def test_incomplete_review_still_mentions_rejected_correction() -> None:
    points = present_document_review(
        [
            "validation_error:text_review_incomplete: contrôle non terminé",
            "validation_error:text_review_invalid_correction:letter_too_long:241",
            "letter_too_long:242",
        ]
    )["points"]

    status = next(point for point in points if point["key"] == "review_status")
    assert "terminé" in status["detail"]
    assert "écartée" in status["detail"]
    assert "241" not in str(points)


def test_distinct_unknown_diagnostics_keep_a_visible_cardinality_without_raw_data() -> None:
    secret = "private passage and credential"
    messages = [f"future_writer_problem:{secret}", f"future_renderer_problem:{secret}"]

    points = present_document_review(messages)["points"]
    copy = " ".join(part for point in points for part in (point["label"], point["detail"]))

    assert len(points) == 2 or "2" in copy
    assert secret not in copy
    assert "future_writer_problem" not in copy
    assert "future_renderer_problem" not in copy


def test_other_actionable_diagnostics_have_specific_short_messages() -> None:
    result = present_document_review(
        [
            "letter_too_short:170",
            "letter_not_3_paragraphs:2",
            "summary_too_long:len=560",
            "summary_too_many_lines:lines=6",
            "internal_evidence_metadata:motivation_letter_body:allowed_claims",
            "text_review_incomplete: contrôle non terminé",
        ]
    )

    assert len(result["points"]) == 6
    assert len({point["key"] for point in result["points"]}) == 6
    details = " ".join(point["detail"] for point in result["points"])
    assert "170 mots" in details
    assert "2 paragraphes" in details
    assert "560 caractères" in details
    assert "6 lignes" in details
    assert all("allowed_claims" not in point["detail"] for point in result["points"])
