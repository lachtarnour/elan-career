"""URL identity functions reject malformed authorities consistently."""

import pytest

from smartapply.offer_identity import (
    direct_offer_url_key,
    is_direct_offer_url,
    normalize_application_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1/jobs/42",
        "https://[invalid]/jobs/42",
        "https://example.com\uff0fjobs/42",
    ],
    ids=["unclosed-ipv6", "invalid-ipv6", "invalid-authority"],
)
def test_malformed_offer_urls_are_rejected(url):
    assert normalize_application_url(url) == ""
    assert is_direct_offer_url(url) is False
    assert direct_offer_url_key(url) is None
