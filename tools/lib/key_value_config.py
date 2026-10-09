# SPDX-License-Identifier: MIT
"""The bounded `KEY=value` reader for `set.conf`, `publisher.conf`, `UPSTREAM.conf` and `asset.conf`.

It reads the grammar ai-tools-base reads every config file with (`conf.lib.sh`), so a value the tools accept is the
value base reads: leading whitespace and whitespace around the key and the `=` are trimmed; a blank line and a `#` line
are skipped; one layer of matched quotes is removed; outside quotes a `#` at the start of the value or after whitespace
ends it; `KEY=` is present with an empty value. A list is `[a, b]`, or a bare `a, b c` split on commas and whitespace;
a bracketed list with one bracket, with quotes around it, or with a quote or a further bracket inside is invalid.

The tools are stricter than base where base reads past a mistake a publisher should see: a line without `=`, a key given
twice, a quote that does not close, text other than whitespace and a `#` comment after the closing quote, and an empty
list item (`[a,,b]`, a trailing comma) are reported, where base takes the last assignment, the text to the line's end
or the items it can split. For a list-typed key, `key=` is refused -- `key=[]` is the explicit empty list and an
absent key is not declared -- so the three are told apart. The reader parses text and does not execute: a caller
reads the file through `safe_read` and hands the text over, and no value in it is evaluated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EMPTY_LIST_REASON = "it is empty; write `key=[]` for an explicit empty list, or omit the key"


@dataclass
class KeyValueDocument:
    """The keys of one file, with what the reader could not accept beside them."""

    values: Dict[str, str] = field(default_factory=dict)
    quoted: Dict[str, bool] = field(default_factory=dict)
    line_numbers: Dict[str, int] = field(default_factory=dict)
    duplicate_keys: List[Tuple[str, int]] = field(default_factory=list)
    malformed_lines: List[Tuple[int, str]] = field(default_factory=list)  # (line number, what is wrong with it)

    def has(self, key: str) -> bool:
        return key in self.values

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def get_list(self, key: str) -> Tuple[Tuple[str, ...], Optional[str]]:
        """The items of a list key, and the reason the list is invalid where it is (the items are then empty)."""
        if key not in self.values:
            return (), None
        return parse_list_value(self.values[key], self.quoted.get(key, False))

    def syntax_errors(self) -> List[str]:
        """One message per thing the reader refused, each naming its line."""
        messages = [f"line {line_number}: {what}" for line_number, what in self.malformed_lines]
        messages.extend(f"line {line_number}: key `{key}` is given again; a key is written once"
                        for key, line_number in self.duplicate_keys)
        return messages


def parse_list_value(value: str, quoted: bool = False) -> Tuple[Tuple[str, ...], Optional[str]]:
    """Split a list value the way base splits it: `[a, b]` bracketed, or bare items on commas and whitespace.

    Returns the items and `None`, or an empty tuple and the reason the value is not a list: an empty value, one
    bracket, quotes around the brackets, a quote or a bracket inside, or an empty item between two commas.
    """
    value = value.strip()
    if not value:
        return (), EMPTY_LIST_REASON
    if value.startswith("[") != value.endswith("]"):
        return (), "it has one bracket and not the other"
    if value.startswith("["):
        if quoted:
            return (), "a bracketed list is written without quotes around it"
        value = value[1:-1]
        if any(character in value for character in "\"'[]"):
            return (), "an item inside brackets carries no quote or bracket"
        if not value.strip():
            return (), None
    items: List[str] = []
    for part in value.split(","):
        if not part.strip():
            return (), "an item between two commas is empty"
        items.extend(part.split())
    return tuple(items), None


def parse_scalar_value(raw_value: str) -> Tuple[str, bool, Optional[str]]:
    """The value after `=`, with one quote layer removed; returns it, whether it was quoted, and what is wrong with it.

    A quoted value closes on its line and is followed by whitespace and a `#` comment at most.
    """
    value = raw_value.lstrip()
    if value[:1] in ("\"", "'"):
        quote = value[0]
        rest = value[1:]
        if quote not in rest:
            return rest.rstrip(), True, f"the {quote} quote does not close"
        closing = rest.index(quote)
        trailing = rest[closing + 1:].strip()
        if trailing and not trailing.startswith("#"):
            return rest[:closing], True, f"`{trailing}` follows the closing quote; a comment after a value opens with `#`"
        return rest[:closing], True, None
    value = strip_inline_comment(value)
    return value.rstrip(), False, None


def strip_inline_comment(value: str) -> str:
    """Cut an unquoted value at a `#` that starts it or follows whitespace."""
    if value.startswith("#"):
        return ""
    for index, character in enumerate(value):
        if character == "#" and value[index - 1].isspace():
            return value[:index]
    return value


def parse_key_value_text(text: str) -> KeyValueDocument:
    """Read every line of a config file's text into a KeyValueDocument."""
    document = KeyValueDocument()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.lstrip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            document.malformed_lines.append((line_number, f"no `=` in `{raw_line.strip()}`"))
            continue
        key, raw_value = line.split("=", 1)
        key = key.rstrip()
        if not KEY_PATTERN.match(key):
            document.malformed_lines.append((line_number, f"`{key}` is not a key of letters, digits and underscores"))
            continue
        value, quoted, problem = parse_scalar_value(raw_value)
        if problem is not None:
            document.malformed_lines.append((line_number, f"`{key}=`: {problem}"))
        if key in document.values:
            document.duplicate_keys.append((key, line_number))
        document.values[key] = value
        document.quoted[key] = quoted
        document.line_numbers[key] = line_number
    return document
