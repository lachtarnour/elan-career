"""Classification copy reflects the stored decision and its actual evidence."""

from __future__ import annotations

import pytest

from smartapply.desktop.classification_explanation import classification_explanation


@pytest.mark.parametrize("components", [None, [], "{}", 1, {"filter_disposition": "unknown"}, {}])
def test_unknown_or_malformed_components_have_no_invented_explanation(components) -> None:
    assert classification_explanation(components) == ("", ())


def test_pending_explains_survival_without_claiming_relevance() -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": "pending_relevance",
            "hard_reject": {"rejected": False, "gates": [{"gate": "role", "status": "clear"}]},
            "reasons": ["offer_language:fr", "search_context:data scientist"],
        }
    )
    assert (
        summary
        == "Aucune incompatibilité explicite n’a été prouvée. La pertinence reste à évaluer."
    )
    assert reasons == ()


def test_pending_shows_only_unresolved_domains_and_ignores_previous_rejections() -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": "pending_relevance",
            "rejection_stage": "manual",
            "rejection_reasons": ["manual_archive"],
            "reasons": ["experience_conflict:technical detail", "search_context:AI"],
            "legacy_filter_migration": {
                "previous_filter_components": {
                    "filter_disposition": "uncertain",
                    "reasons": ["below_min_score"],
                }
            },
            "hard_reject": {
                "gates": [
                    {"gate": "role", "status": "unresolved", "conflicts": ["title conflict"]},
                    {
                        "gate": "experience",
                        "status": "unresolved",
                        "reason_codes": ["below_min_score"],
                    },
                    {"gate": "role", "status": "unresolved"},
                    {"gate": "contract", "status": "clear", "notes": ["CDI"]},
                ]
            },
        }
    )
    assert "pertinence reste à évaluer" in summary
    assert reasons == ("Nature du poste à vérifier.", "Exigence d’expérience à vérifier.")


def test_manual_pending_override_is_truthful_when_a_blocker_was_recorded() -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": "pending_relevance",
            "reasons": ["Manual override: kept by user"],
            "hard_reject": {
                "rejected": True,
                "gates": [
                    {
                        "gate": "experience",
                        "status": "blocked",
                        "reason_codes": ["experience_required_too_high:5+ years"],
                    },
                ],
            },
        }
    )
    assert summary == "Offre conservée par décision manuelle ; la pertinence reste à évaluer."
    assert reasons == ()


def test_pending_with_contradictory_stored_blocker_does_not_claim_no_blocker() -> None:
    summary, _ = classification_explanation(
        {
            "filter_disposition": "pending_relevance",
            "hard_reject": {"gates": [{"gate": "experience", "status": "blocked"}]},
        }
    )
    assert "contradictoires" in summary


def test_rejected_uses_blocked_codes_and_their_evidence_only() -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": "rejected",
            "reasons": ["search_context:AI", "role_conflict:ambiguous", "below_min_score"],
            "rejection_reasons": ["manual_archive"],
            "hard_reject": {
                "rejected": True,
                "gates": [
                    {
                        "gate": "experience",
                        "status": "blocked",
                        "reason_codes": ["experience_required_too_high:5+ years"],
                        "notes": ["contract_off:CDD"],
                        "conflicts": ["not decisive"],
                        "evidence": [{"quote": "  5 ans\n minimum exigés.  "}],
                    },
                    {
                        "gate": "role",
                        "status": "unresolved",
                        "reason_codes": ["off_target_role_family:sales_commercial"],
                        "evidence": [{"quote": "unresolved quote"}],
                    },
                    {
                        "gate": "contract",
                        "status": "clear",
                        "reason_codes": ["blocked_contract_type:CDD"],
                        "evidence": [{"quote": "clear quote"}],
                    },
                ],
            },
        }
    )
    assert summary == "Au moins un critère de rejet est établi."
    assert reasons == (
        "Expérience demandée trop élevée : au moins 5 ans",
        "Extrait — expérience : « 5 ans minimum exigés. »",
    )


def test_blocked_language_context_is_not_a_rejection_reason() -> None:
    _, reasons = classification_explanation(
        {
            "filter_disposition": "rejected",
            "hard_reject": {
                "gates": [
                    {
                        "gate": "language",
                        "status": "blocked",
                        "reason_codes": ["offer_language:de", "offer_language_not_accepted:de"],
                    }
                ]
            },
        }
    )
    assert reasons == ("Langue de l’offre non prise en charge : allemand",)


def test_quotes_are_bounded_and_not_full_offer_text() -> None:
    _, reasons = classification_explanation(
        {
            "filter_disposition": "rejected",
            "hard_reject": {
                "gates": [
                    {
                        "gate": "qualification",
                        "status": "blocked",
                        "reason_codes": ["qualification_required:doctorate"],
                        "evidence": [{"quote": "Doctorat obligatoire. " * 100}],
                    }
                ]
            },
        }
    )
    assert reasons[0] == "Doctorat obligatoire non compatible avec le profil actuel"
    assert len(reasons[1]) < 220
    assert "…" in reasons[1]


def test_legacy_reject_uses_persisted_decisive_reasons() -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": "rejected",
            "rejection_reasons": ["title_hard_reject:sales_commercial"],
            "reasons": ["offer_language:fr", "below_min_score"],
        }
    )
    assert summary == "Décision de rejet historique enregistrée."
    assert reasons == ("Poste commercial ou vente, hors cible Data/IA",)


def test_legacy_reject_fallback_excludes_context_conflicts_and_positive_signals() -> None:
    _, reasons = classification_explanation(
        {
            "filter_disposition": "rejected",
            "reasons": [
                "offer_language:fr",
                "search_context:AI",
                "rome_context:data",
                "positive_title:AI",
                "experience_evidence_conflict:missing",
                "role_title_veto:AI mission",
                "Manual override: kept by user",
                "blocked_contract_type:stage",
            ],
        }
    )
    assert reasons == ("Type de contrat non recherché : stage",)


@pytest.mark.parametrize("disposition", ["relevant", "uncertain"])
@pytest.mark.parametrize(
    "hard_reject",
    [
        None,
        {"rejected": False, "gates": [{"gate": "role", "status": "clear"}]},
    ],
)
def test_legacy_relevance_labels_await_current_evaluations(
    disposition: str,
    hard_reject,
) -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": disposition,
            "reasons": [
                "positive_title:machine learning",
                "role_evidence:candidate_mission",
                "role_evidence:target_title:data scientist",
                "role_evidence:core_tool_in_mission",
                "role_evidence:incidental_or_company_only_concept",
                "role_relevance_score:8",
                "role_concepts:machine_learning",
                "contract_ok:cdi",
                "search_context:AI",
            ],
            "hard_reject": hard_reject,
            "legacy_filter_migration": {
                "previous_filter_components": {
                    "filter_disposition": "relevant",
                    "reasons": ["positive_title:machine learning"],
                }
            },
        }
    )
    assert summary == (
        "Ancienne classification conservée dans l’historique ; l’offre attend "
        "l’évaluation du nouveau filtre de rejet, puis de sa pertinence."
    )
    assert "Aucune incompatibilité explicite" not in summary
    assert reasons == ()


@pytest.mark.parametrize(
    "hard_reject",
    [
        None,
        [],
        "bad",
        {"gates": None},
        {"gates": "bad"},
        {
            "gates": [
                None,
                "bad",
                2,
                {"status": "blocked", "reason_codes": None, "evidence": "bad"},
            ],
        },
    ],
)
def test_malformed_gate_payloads_are_safe(hard_reject) -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": "rejected",
            "hard_reject": hard_reject,
            "reasons": [None, 4, {"bad": True}],
        }
    )
    assert isinstance(summary, str)
    assert reasons == ()


def test_presentation_never_mutates_persisted_components() -> None:
    components = {
        "filter_disposition": "pending_relevance",
        "hard_reject": {"gates": [{"gate": "role", "status": "unresolved"}]},
    }
    classification_explanation(components)
    assert components == {
        "filter_disposition": "pending_relevance",
        "hard_reject": {"gates": [{"gate": "role", "status": "unresolved"}]},
    }


def test_malformed_gate_names_do_not_crash_presentation() -> None:
    summary, reasons = classification_explanation(
        {
            "filter_disposition": "pending_relevance",
            "hard_reject": {"gates": [{"gate": [], "status": "unresolved"}]},
        }
    )
    assert "pertinence reste à évaluer" in summary
    assert reasons == ()
    _, reasons = classification_explanation(
        {
            "filter_disposition": "rejected",
            "hard_reject": {
                "gates": [
                    {
                        "gate": [],
                        "status": "blocked",
                        "reason_codes": ["qualification_required:doctorate"],
                        "evidence": [{"quote": "Doctorat obligatoire"}],
                    }
                ]
            },
        }
    )
    assert reasons[1] == "Extrait — critère : « Doctorat obligatoire »"
