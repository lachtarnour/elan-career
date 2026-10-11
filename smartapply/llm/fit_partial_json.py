"""Read a valid JSON prefix without completing tokens or discarding duplicate keys.

Only EOF can end recovery. A syntax error before EOF is never repaired. Containers
retain their read children; unfinished scalar values are omitted and reported to
our object factory so an unfinished duplicate scoring key remains ambiguous.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_MISSING = object()
_DELIMITERS = " \t\r\n,]}"
_DIGITS = "0123456789"


@dataclass(frozen=True)
class JSONKeyPrefix:
    """Decoded characters and any unfinished escape in an interrupted key."""

    decoded: str
    pending_escape: str = ""

    def could_match(self, key: str) -> bool:
        if not key.startswith(self.decoded):
            return False
        if not self.pending_escape:
            return True
        remaining = key[len(self.decoded) :]
        if not remaining:
            return False
        if self.pending_escape == "\\":
            # Any next character can still be encoded with a Unicode escape.
            return True
        codepoint = ord(remaining[0])
        if codepoint > 0xFFFF:
            codepoint = 0xD800 + ((codepoint - 0x10000) >> 10)
        return f"{codepoint:04x}".startswith(self.pending_escape[2:].lower())


@dataclass(frozen=True)
class _Value:
    value: Any = _MISSING
    complete: bool = False
    key_prefix: JSONKeyPrefix | None = None


class _PrefixReader:
    def __init__(self, text: str, object_factory: Callable):
        self.text = text
        self.position = 0
        self.object_factory = object_factory

    def peek(self) -> str:
        return self.text[self.position : self.position + 1]

    def whitespace(self):
        while self.peek() and self.peek() in " \t\r\n":
            self.position += 1

    def value(self) -> _Value:
        self.whitespace()
        char = self.peek()
        if not char:
            return _Value()
        if char == "{":
            return self.object()
        if char == "[":
            return self.array()
        if char == '"':
            return self.string()
        if char in "-0123456789":
            return self.number()
        for literal, value in (("true", True), ("false", False), ("null", None)):
            if char == literal[0]:
                suffix = self.text[self.position :]
                if literal.startswith(suffix) and len(suffix) < len(literal):
                    self.position = len(self.text)
                    return _Value()
                if not suffix.startswith(literal):
                    raise ValueError("Invalid JSON literal")
                self.position += len(literal)
                if self.peek() and self.peek() not in _DELIMITERS:
                    raise ValueError("Invalid JSON literal boundary")
                return _Value(value, True)
        raise ValueError("Invalid JSON value")

    def string(self) -> _Value:
        start = self.position
        self.position += 1
        while char := self.peek():
            self.position += 1
            if char == '"':
                return _Value(json.loads(self.text[start : self.position]), True)
            if ord(char) < 32:
                raise ValueError("Invalid JSON string control character")
            if char != "\\":
                continue
            escape_start = self.position - 1
            escape = self.peek()
            if not escape:
                return self.unfinished_string(start, escape_start)
            self.position += 1
            if escape == "u":
                digits = self.text[self.position : self.position + 4]
                if any(digit not in "0123456789abcdefABCDEF" for digit in digits):
                    raise ValueError("Invalid JSON unicode escape")
                self.position += len(digits)
                if len(digits) < 4:
                    return self.unfinished_string(start, escape_start)
            elif escape not in '\\"/bfnrt':
                raise ValueError("Invalid JSON escape")
        return self.unfinished_string(start, self.position)

    def unfinished_string(self, start: int, escape_start: int) -> _Value:
        # Decode only complete characters; never complete the pending escape.
        decoded = json.loads(self.text[start:escape_start] + '"')
        pending_escape = self.text[escape_start : self.position]
        return _Value(key_prefix=JSONKeyPrefix(decoded, pending_escape))

    def number(self) -> _Value:
        start = self.position
        if self.peek() == "-":
            self.position += 1
        if not self.peek():
            return _Value()
        if self.peek() == "0":
            self.position += 1
        elif self.peek() in "123456789":
            self.digits()
        else:
            raise ValueError("Invalid JSON number")
        for marker in (".", "eE"):
            if not self.peek() or self.peek() not in marker:
                continue
            self.position += 1
            if marker == "eE" and self.peek() and self.peek() in "+-":
                self.position += 1
            if not self.peek():
                return _Value()
            if self.peek() not in _DIGITS:
                raise ValueError("Invalid JSON number component")
            self.digits()
        # EOF does not delimit a number: `3` could be the prefix of `3.5`.
        if not self.peek():
            return _Value()
        if self.peek() not in _DELIMITERS:
            raise ValueError("Invalid JSON number boundary")
        return _Value(json.loads(self.text[start : self.position]), True)

    def digits(self):
        while self.peek() and self.peek() in _DIGITS:
            self.position += 1

    def object(self) -> _Value:
        self.position += 1
        pairs, incomplete_keys = [], []
        unread_key_prefix = None
        self.whitespace()
        if self.peek() == "}":
            self.position += 1
            return _Value(self.object_factory(pairs), True)
        while self.peek():
            if self.peek() != '"':
                raise ValueError("Invalid JSON object key")
            key = self.string()
            if not key.complete:
                unread_key_prefix = key.key_prefix
                break
            self.whitespace()
            if not self.peek():
                incomplete_keys.append(key.value)
                break
            if self.peek() != ":":
                raise ValueError("Missing JSON colon")
            self.position += 1
            value = self.value()
            if value.value is _MISSING:
                incomplete_keys.append(key.value)
            else:
                pairs.append((key.value, value.value))
            if not value.complete:
                break
            self.whitespace()
            char = self.peek()
            if char == "}":
                self.position += 1
                return _Value(self.object_factory(pairs), True)
            if not char:
                break
            if char != ",":
                raise ValueError("Missing JSON object separator")
            self.position += 1
            self.whitespace()
        return _Value(
            self.object_factory(
                pairs, incomplete_keys=incomplete_keys, unread_key_prefix=unread_key_prefix
            )
        )

    def array(self) -> _Value:
        self.position += 1
        items = []
        self.whitespace()
        if self.peek() == "]":
            self.position += 1
            return _Value(items, True)
        while self.peek():
            value = self.value()
            if value.value is not _MISSING:
                items.append(value.value)
            if not value.complete:
                break
            self.whitespace()
            char = self.peek()
            if char == "]":
                self.position += 1
                return _Value(items, True)
            if not char:
                break
            if char != ",":
                raise ValueError("Missing JSON array separator")
            self.position += 1
            self.whitespace()
        return _Value(items)


def recover_truncated_json(text: str, *, object_factory: Callable):
    reader = _PrefixReader(text, object_factory)
    parsed = reader.value()
    reader.whitespace()
    if parsed.complete or parsed.value is _MISSING or reader.position != len(text):
        raise ValueError("Response is not a recoverable truncated JSON container")
    return parsed.value
