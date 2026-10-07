"""Structured compensation evidence used by the contract gate.

An ambiguous source label such as ``Contract`` is not enough to prove that an
offer is freelance.  A clear daily rate in a structured pay field is useful
corroboration; unrelated free text and hourly salaries are deliberately
ignored here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from smartapply.filtering.text import norm

_PAY_FIELD_RE = re.compile(
    r"(?:^|_)(?:salary|compensation|pay|wage|remuneration|tjm|tarif|billing)"
    r"(?:_|$)"
)
_PAY_PERIOD_FIELD_RE = re.compile(
    r"(?:^|_)(?:period|frequency|interval|unit|cadence)(?:_|$)"
)
_PAY_AMOUNT_FIELD_RE = re.compile(
    r"(?:^|_)(?:amount|value|min|minimum|max|maximum|from|to|salary|pay|wage|"
    r"rate|tjm)(?:_|$)"
)
_DAILY_RATE_RE = re.compile(
    r"(?:\b(?:daily|journalier|journaliere|tjm)\b|"
    r"(?:/|\bper\s+|\bpar\s+|\ban?\s+)(?:day|jour)s?\b)"
)
_NUMERIC_AMOUNT_RE = re.compile(r"(?<!\d)\d{1,6}(?:[.,]\d{1,2})?(?![a-z0-9])")


def structured_daily_pay_path(value: Any) -> str | None:
    """Return the source-data path for a clear structured daily rate."""

    return _find_structured_pay_path(
        value,
        path=("source_data",),
        pay_context=False,
        depth=0,
    )


def _find_structured_pay_path(
    value: Any,
    *,
    path: tuple[str, ...],
    pay_context: bool,
    depth: int,
) -> str | None:
    if depth > 6:
        return None

    if isinstance(value, Mapping):
        items = [(str(key), nested) for key, nested in value.items()]

        for key, nested in items:
            key_words = _structured_key_words(key)
            nested_path = (*path, key)
            if (
                _is_explicit_daily_rate_field(key_words)
                and _contains_numeric_amount(nested)
            ):
                return ".".join(nested_path)
            if _PAY_FIELD_RE.search(key_words) and _is_clear_daily_rate_text(nested):
                return ".".join(nested_path)

        period_items = [
            (key, nested)
            for key, nested in items
            if _PAY_PERIOD_FIELD_RE.search(_structured_key_words(key))
            and _has_daily_unit(nested)
        ]
        amount_items = [
            (key, nested)
            for key, nested in items
            if _PAY_AMOUNT_FIELD_RE.search(_structured_key_words(key))
            and (pay_context or _PAY_FIELD_RE.search(_structured_key_words(key)))
            and _contains_numeric_amount(nested)
        ]
        if period_items and amount_items:
            return ".".join((*path, period_items[0][0]))

        for key, nested in items:
            key_words = _structured_key_words(key)
            nested_pay_context = pay_context or bool(_PAY_FIELD_RE.search(key_words))
            found = _find_structured_pay_path(
                nested,
                path=(*path, key),
                pay_context=nested_pay_context,
                depth=depth + 1,
            )
            if found:
                return found
        return None

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            found = _find_structured_pay_path(
                nested,
                path=(*path, str(index)),
                pay_context=pay_context,
                depth=depth + 1,
            )
            if found:
                return found
        return None

    if pay_context and _is_clear_daily_rate_text(value):
        return ".".join(path)
    return None


def _structured_key_words(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", norm(separated)).strip("_")


def _is_explicit_daily_rate_field(key_words: str) -> bool:
    tokens = set(filter(None, key_words.split("_")))
    period_tokens = {"daily", "journalier", "journaliere"}
    pay_tokens = {"salary", "pay", "wage", "rate", "tjm", "tarif"}
    return key_words == "tjm" or bool(tokens & period_tokens and tokens & pay_tokens)


def _is_clear_daily_rate_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = norm(value)
    return bool(_DAILY_RATE_RE.search(normalized) and _NUMERIC_AMOUNT_RE.search(normalized))


def _has_daily_unit(value: Any) -> bool:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return False
    normalized = norm(str(value)).strip()
    if normalized in {"day", "days", "jour", "jours"}:
        return True
    return bool(_DAILY_RATE_RE.search(normalized))


def _contains_numeric_amount(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return bool(_NUMERIC_AMOUNT_RE.search(norm(value)))
    return False


__all__ = ["structured_daily_pay_path"]
