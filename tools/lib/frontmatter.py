# SPDX-License-Identifier: MIT
"""The bounded reader for the YAML frontmatter of a `SKILL.md` and of a subagent file.

The format admits a subset of YAML a reader can parse without ambiguity, and this module is that subset's one
statement, so the tools and base refuse the same files. A frontmatter opens with `---` on the first line and closes with
the next `---` line. Inside it, a line is blank, a `#` comment, or `key: value` at the margin with a key of letters,
digits, `-` and `_`. A value is a plain scalar, a `"double"` or `'single'` quoted scalar on the one line, a flow list
`[a, b]` of plain scalars, or empty, in which case the lines indented under the key form either a map of
`key: scalar` lines (one level, for `metadata`) or a sequence of `- item` lines (for `tools` and `skills`).

Refused, each with the line that carries it: a tab in indentation, a block scalar (`|`, `>`), an anchor, an alias,
a tag, a flow map, a complex key, a multi-line plain scalar, a nesting deeper than one level, a key given twice, and
a frontmatter that does not close. A refused file reports every error the reader met, so a publisher fixes them in one
pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

KEY_LINE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*):(?:\s+(?P<value>.*))?$")
INDENTED_KEY_LINE = re.compile(r"^(?P<indent> +)(?P<key>[A-Za-z][A-Za-z0-9_-]*):(?:\s+(?P<value>.*))?$")
SEQUENCE_ITEM_LINE = re.compile(r"^(?P<indent> +)-\s+(?P<value>.+)$")
FRONTMATTER_DELIMITER = re.compile(r"^---\s*$")
REFUSED_SCALAR_OPENERS = ("|", ">", "&", "*", "!", "{", "?")

FrontmatterValue = Union[str, List[str], Dict[str, str]]


@dataclass
class FrontmatterDocument:
    """The keys of one frontmatter, the line after its closing `---`, and the errors the reader met."""

    values: Dict[str, FrontmatterValue] = field(default_factory=dict)
    body_start_line: int = 0
    errors: List[str] = field(default_factory=list)
    present: bool = False

    def has_errors(self) -> bool:
        return bool(self.errors)


def parse_scalar(raw_value: str, line_number: int, errors: List[str]) -> Optional[str]:
    """One scalar on one line: plain (a ` #` comment cut), or quoted whole; returns None after recording an error."""
    value = raw_value.strip()
    if not value:
        return ""
    if value[0] in ("\"", "'"):
        quote = value[0]
        closing = find_closing_quote(value, quote)
        if closing is None:
            errors.append(f"line {line_number}: the quoted value does not close on its line")
            return None
        trailing = value[closing + 1:].strip()
        if trailing and not trailing.startswith("#"):
            errors.append(f"line {line_number}: text follows the closing quote: `{trailing}`")
            return None
        inner = value[1:closing]
        return inner.replace("''", "'") if quote == "'" else inner.replace('\\"', '"').replace("\\\\", "\\")
    if value[0] in REFUSED_SCALAR_OPENERS:
        errors.append(f"line {line_number}: a value opening with `{value[0]}` is outside the accepted subset")
        return None
    if value.startswith("[") or value.endswith("]"):
        errors.append(f"line {line_number}: a flow list is accepted only as a whole `[a, b]` value of a top-level key")
        return None
    comment = re.search(r"\s#", value)
    if comment:
        value = value[: comment.start()].rstrip()
    return value


def find_closing_quote(value: str, quote: str) -> Optional[int]:
    """The index of the quote that closes the scalar opened at index 0, honouring the quote's own escape."""
    index = 1
    while index < len(value):
        character = value[index]
        if quote == '"' and character == "\\":
            index += 2
            continue
        if character == quote:
            if quote == "'" and value[index + 1: index + 2] == "'":
                index += 2
                continue
            return index
        index += 1
    return None


def parse_flow_list(raw_value: str, line_number: int, errors: List[str]) -> Optional[List[str]]:
    """`[a, b]` of plain scalars; quotes and nested brackets are refused."""
    inner = raw_value.strip()[1:-1]
    if any(character in inner for character in "[]{}\"'"):
        errors.append(f"line {line_number}: a flow list holds plain items alone")
        return None
    return [item.strip() for item in inner.split(",") if item.strip()]


def parse_frontmatter(text: str) -> FrontmatterDocument:
    """Read the frontmatter at the start of `text`; `present` is False when the text does not open with `---`."""
    document = FrontmatterDocument()
    lines = text.split("\n")
    if not lines or not FRONTMATTER_DELIMITER.match(lines[0]):
        return document
    document.present = True
    index = 1
    current_key: Optional[str] = None
    current_indent: Optional[int] = None
    while index < len(lines):
        line = lines[index]
        line_number = index + 1
        if FRONTMATTER_DELIMITER.match(line):
            document.body_start_line = line_number + 1
            return document
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            document.errors.append(f"line {line_number}: a tab in the indentation")
            index += 1
            continue
        top_level = KEY_LINE.match(line)
        if top_level:
            key = top_level.group("key")
            raw_value = top_level.group("value")
            if key in document.values:
                document.errors.append(f"line {line_number}: key `{key}` is given again; a key is written once")
            current_key, current_indent = None, None
            if raw_value is None or not raw_value.strip() or raw_value.strip().startswith("#"):
                document.values[key] = ""
                current_key = key
            elif raw_value.strip().startswith("[") and raw_value.strip().endswith("]"):
                items = parse_flow_list(raw_value, line_number, document.errors)
                document.values[key] = items if items is not None else []
            else:
                scalar = parse_scalar(raw_value, line_number, document.errors)
                document.values[key] = scalar if scalar is not None else ""
            index += 1
            continue
        nested_key = INDENTED_KEY_LINE.match(line)
        sequence_item = SEQUENCE_ITEM_LINE.match(line)
        if current_key is None or (nested_key is None and sequence_item is None):
            document.errors.append(f"line {line_number}: `{stripped}` is not `key: value`, `- item` under a key, or a comment")
            index += 1
            continue
        indent = len((nested_key or sequence_item).group("indent"))
        if current_indent is None:
            current_indent = indent
        elif indent != current_indent:
            document.errors.append(f"line {line_number}: the indentation changes inside `{current_key}`; one level is read")
            index += 1
            continue
        existing = document.values[current_key]
        if nested_key:
            if existing == "":
                existing = {}
                document.values[current_key] = existing
            if not isinstance(existing, dict):
                document.errors.append(f"line {line_number}: `{current_key}` mixes a map and a sequence")
            else:
                map_key = nested_key.group("key")
                if map_key in existing:
                    document.errors.append(f"line {line_number}: `{current_key}.{map_key}` is given again")
                raw_value = nested_key.group("value")
                if raw_value is None or not raw_value.strip():
                    document.errors.append(f"line {line_number}: `{current_key}.{map_key}` has no value; one level is read")
                else:
                    scalar = parse_scalar(raw_value, line_number, document.errors)
                    existing[map_key] = scalar if scalar is not None else ""
        else:
            if existing == "":
                existing = []
                document.values[current_key] = existing
            if not isinstance(existing, list):
                document.errors.append(f"line {line_number}: `{current_key}` mixes a sequence and a map")
            else:
                scalar = parse_scalar(sequence_item.group("value"), line_number, document.errors)
                if scalar is not None:
                    existing.append(scalar)
        index += 1
    document.errors.append("the frontmatter does not close with `---`")
    document.body_start_line = len(lines) + 1
    return document


def split_frontmatter(text: str) -> Tuple[FrontmatterDocument, str]:
    """The frontmatter and the body after it; the body is the whole text when no frontmatter opens it."""
    document = parse_frontmatter(text)
    if not document.present or document.body_start_line == 0:
        return document, text
    return document, "\n".join(text.split("\n")[document.body_start_line - 1:])
