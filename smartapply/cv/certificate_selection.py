"""Certificate selection shared by validation and CV rendering."""

from __future__ import annotations

from collections import Counter

from smartapply.profile import Certificate, Profile

MAX_CERTIFICATES = 2


def certificate_policy_errors(selected_ids: list[str] | None, profile: Profile) -> list[str]:
    """Validate generated IDs; None is reserved for historical CV snapshots."""
    if selected_ids is None:
        return []
    errors: list[str] = []
    known_ids = {certificate.id for certificate in profile.certificates}
    if len(selected_ids) > MAX_CERTIFICATES:
        errors.append(f"too_many_certificates:max={MAX_CERTIFICATES}")
    for certificate_id, count in Counter(selected_ids).items():
        if count > 1:
            errors.append(f"duplicate_certificate_id:{certificate_id}")
        if certificate_id not in known_ids:
            errors.append(f"unknown_certificate_id:{certificate_id}")
    return errors


def selected_certificates(profile: Profile, selected_ids: list[str] | None) -> list[Certificate]:
    """Preserve legacy display or follow the finalized selection order."""
    if selected_ids is None:
        return list(profile.certificates)
    by_id = {certificate.id: certificate for certificate in profile.certificates}
    return [by_id[certificate_id] for certificate_id in selected_ids if certificate_id in by_id]
