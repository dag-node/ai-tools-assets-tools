# SPDX-License-Identifier: MIT
"""The bounded `KEY=value` reader for `set.conf`, `publisher.conf`, `UPSTREAM.conf` and `asset.conf`.

It reads the grammar ai-tools-base reads every config file with (`conf.lib.sh`), so a value the tools accept is the
value base reads: leading whitespace and whitespace around the key and the `=` are trimmed; a blank line and a `#` line
are skipped; one layer of matched quotes is removed; outside quotes a `#` at the start of the value or after whitespace
ends it; `KEY=` is present with an empty value. A list is `[a, b]`, or a bare `a, b c` split on commas and whitespace;
a bracketed list with one bracket, with quotes around it, or with a quote or a further bracket inside is invalid.

The tools are stricter than base in two places, each a mistake base reads past and a publisher should see: a line
without `=` and a key given twice are reported, where base ignores the line and takes the last assignment. The reader
parses and does not execute: a file is text, read whole, and no value in it is evaluated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LIST_SEPARATORS = re.compile(r"[,\s]+")


@dataclass
class ConfigDocument:
    """The keys of one file, with what the reader could not accept beside them."""

    values: Dict[str, str] = field(default_factory=dict)
    quoted: Dict[str, bool] = field(default_factory=dict)
    line_numbers: Dict[str, int] = field(default_factory=dict)
    duplicate_keys: List[Tuple[str, int]] = field(default_factory=list)
    malformed_lines: List[Tuple[int, str]] = field(default_factory=list)

    def has(self, key: str) -> bool:
        return key in self.values

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def list_value(self, key: str) -> Tuple[Tuple[str, ...], Optional[str]]:
        """The items of a list key, and the reason the list is invalid where it is (the items are then empty)."""
        if key not in self.values:
            return (), None
        return parse_list_value(self.values[key], self.quoted.get(key, False))

    def syntax_errors(self) -> List[str]:
        """One message per thing the reader refused, each naming its line."""
        messages = [f"line {line_number}: no `=` in `{text}`" for line_number, text in self.malformed_lines]
        messages.extend(f"line {line_number}: key `{key}` is given again; a key is written once"
                        for key, line_number in self.duplicate_keys)
        return messages


def parse_list_value(value: str, quoted: bool = False) -> Tuple[Tuple[str, ...], Optional[str]]:
    """Split a list value the way base splits it: `[a, b]` bracketed, or bare items on commas and whitespace.

    Returns the items and `None`, or an empty tuple and the reason the value is not a list.
    """
    value = value.strip()
    if not value.startswith("[") and not value.endswith("]"):
        return tuple(item for item in LIST_SEPARATORS.split(value) if item), None
    if not (value.startswith("[") and value.endswith("]")):
        return (), "it has one bracket and not the other"
    if quoted:
        return (), "a bracketed list is written without quotes around it"
    inner = value[1:-1]
    if any(character in inner for character in "\"'[]"):
        return (), "an item inside brackets carries no quote or bracket"
    return tuple(item for item in LIST_SEPARATORS.split(inner) if item), None


def parse_scalar_value(raw_value: str) -> Tuple[str, bool]:
    """The value after `=`, with one quote layer removed; returns it and whether it was quoted."""
    value = raw_value.lstrip()
    if value[:1] in ("\"", "'"):
        quote = value[0]
        rest = value[1:]
        if quote in rest:
            return rest[: rest.index(quote)], True
        return rest.rstrip(), True
    value = strip_inline_comment(value)
    return value.rstrip(), False


def strip_inline_comment(value: str) -> str:
    """Cut an unquoted value at a `#` that starts it or follows whitespace."""
    if value.startswith("#"):
        return ""
    for index, character in enumerate(value):
        if character == "#" and value[index - 1].isspace():
            return value[:index]
    return value


def parse_key_value_text(text: str) -> ConfigDocument:
    """Read every line of a config file's text into a ConfigDocument."""
    document = ConfigDocument()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.lstrip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            document.malformed_lines.append((line_number, raw_line.strip()))
            continue
        key, raw_value = line.split("=", 1)
        key = key.rstrip()
        if not KEY_PATTERN.match(key):
            document.malformed_lines.append((line_number, raw_line.strip()))
            continue
        value, quoted = parse_scalar_value(raw_value)
        if key in document.values:
            document.duplicate_keys.append((key, line_number))
        document.values[key] = value
        document.quoted[key] = quoted
        document.line_numbers[key] = line_number
    return document


def read_key_value_file(path: Path) -> ConfigDocument:
    """Read a config file; the caller handles OSError and UnicodeDecodeError, which name the file as unreadable."""
    return parse_key_value_text(path.read_text(encoding="utf-8"))
