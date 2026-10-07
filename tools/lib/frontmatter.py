# SPDX-License-Identifier: MIT
"""The bounded reader for the YAML frontmatter of a `SKILL.md` and of a subagent file.

The format accepts a subset of YAML a reader can parse without ambiguity, and this module is that subset's one
statement, so the tools and base refuse the same files. A frontmatter opens with `---` on the first line and closes with
the next `---` line. Inside it, a line is blank, a `#` comment, or `key: value` at the margin with a key of letters,
digits, `-` and `_`. A value is a plain scalar, a `"double"` or `'single'` quoted scalar on the one line, a flow list
`[a, b]` of plain scalars, or empty, in which case the lines indented under the key form either a map of
`key: scalar` lines (one level, for `metadata`) or a sequence of `- item` lines (for `tools` and `skills`).

A scalar is kept with whether it was quoted, since the typing of a field (`asset_format` declares one per field, and
`set_validation` applies it) turns on that: a plain scalar another YAML reader would type -- `true`, `no`, `null`,
`~`, an integer (a sexagesimal `1:20` included), a float, a date or a timestamp -- is refused in a string field with
"quote it", and an integer field takes a plain integer alone. A key with no value, or a `#` comment alone, is an
omitted scalar (`Scalar("")`, unquoted, which YAML reads as null) and is told apart from the quoted empty string `""`.
One lexical check holds every plain scalar, wherever it stands -- a top-level value, a flow-list item, a sequence item,
a map value: it opens with no indicator (`| > & * ! { ? @ ` % #`, or `-` then a space), does not carry `: ` or `:`
then a tab, which another reader takes as a mapping, nor ` #` or a tab then `#`, which it takes as a comment; and in a
flow list it carries no `,`, bracket, brace or quote. A double-quoted scalar escapes `\\` and `\"` alone; a
single-quoted scalar escapes `''` alone.

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
REFUSED_SCALAR_OPENERS = ("|", ">", "&", "*", "!", "{", "?", "@", "`", "%", "#")
FLOW_ITEM_REFUSED_CHARACTERS = ",[]{}\"'"
# What YAML 1.1 and 1.2 readers type when the scalar is plain: booleans in every spelling and case, null, integers in
# the decimal, octal, hex, underscore and YAML 1.1 sexagesimal forms, floats with an exponent or a sexagesimal part,
# `.inf` and `.nan`, and the YAML 1.1 timestamp forms, a bare date included.
YAML_TYPED_PLAIN = re.compile(
    r"^(?:true|false|yes|no|on|off|y|n|null|~"
    r"|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0o?[0-7_]+|[-+]?0x[0-9a-fA-F_]+|[-+]?0b[01_]+"
    r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+"
    r"|[-+]?(?:[0-9][0-9_]*)?\.[0-9_]*(?:[eE][-+]?[0-9]+)?|[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?[eE][-+]?[0-9]+"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?"
    r")$", re.IGNORECASE)


@dataclass(frozen=True)
class Scalar:
    """One scalar as written: its text with the quotes removed and the escapes resolved, and whether it was quoted."""

    text: str
    quoted: bool = False

    @property
    def is_yaml_typed(self) -> bool:
        """True for a plain scalar another YAML reader reads as a boolean, null, a number, a date or a timestamp."""
        return not self.quoted and bool(YAML_TYPED_PLAIN.match(self.text))

    @property
    def is_omitted(self) -> bool:
        """True for a key given no value (or a comment alone), which YAML reads as null; `""` quoted is not omitted."""
        return not self.quoted and self.text == ""


FrontmatterValue = Union[Scalar, List[Scalar], Dict[str, Scalar]]


@dataclass
class FrontmatterDocument:
    """The keys of one frontmatter, the line after its closing `---`, and the errors the reader met."""

    values: Dict[str, FrontmatterValue] = field(default_factory=dict)
    body_start_line: int = 0
    errors: List[str] = field(default_factory=list)
    present: bool = False

    def has_errors(self) -> bool:
        return bool(self.errors)


def is_omitted_value(raw_value: Optional[str]) -> bool:
    """True for no value after the key, or a `#` comment alone: YAML's null, this subset's omitted scalar."""
    return raw_value is None or not raw_value.strip() or raw_value.strip().startswith("#")


def parse_scalar(raw_value: str, line_number: int, errors: List[str]) -> Optional[Scalar]:
    """One scalar on one line, plain or quoted whole; returns None after recording an error."""
    value = raw_value.strip()
    if is_omitted_value(value):
        return Scalar("")
    if value[0] in ("\"", "'"):
        return parse_quoted_scalar(value, line_number, errors)
    if value.startswith("[") or value.endswith("]"):
        errors.append(f"line {line_number}: a flow list is accepted only as a whole `[a, b]` value of a top-level key")
        return None
    if not check_plain_lexeme(value, line_number, errors):
        return None
    return Scalar(value)


def check_plain_lexeme(text: str, line_number: int, errors: List[str], flow: bool = False) -> bool:
    """True when `text` is a plain scalar this subset reads, wherever it stands; False after recording why another
    YAML reader would read it as something else -- an opening indicator, a mapping separator, a comment, and in a flow
    list a `,`, a bracket, a brace or a quote anywhere."""
    opener = text[0]
    if opener in REFUSED_SCALAR_OPENERS or (opener == "-" and text[1:2] in (" ", "\t", "")):
        errors.append(f"line {line_number}: a value opening with `{opener}` is outside the accepted subset")
        return False
    if ": " in text or ":\t" in text or text.endswith(":"):
        errors.append(f"line {line_number}: a plain scalar does not carry `: ` (or `:` then a tab), which a YAML reader takes as a mapping; quote it")
        return False
    if " #" in text or "\t#" in text:
        errors.append(f"line {line_number}: a plain scalar does not carry ` #` (or a tab then `#`), which a YAML reader takes as a comment; quote it")
        return False
    if flow and any(character in text for character in FLOW_ITEM_REFUSED_CHARACTERS):
        errors.append(f"line {line_number}: a flow list holds plain items alone; `{text}` carries a `,`, a bracket, a brace or a quote")
        return False
    return True


def parse_quoted_scalar(value: str, line_number: int, errors: List[str]) -> Optional[Scalar]:
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
    if quote == "'":
        return Scalar(inner.replace("''", "'"), quoted=True)
    unescaped: List[str] = []
    index = 0
    while index < len(inner):
        character = inner[index]
        if character == "\\":
            escaped = inner[index + 1: index + 2]
            if escaped not in ("\\", "\""):
                errors.append(f"line {line_number}: `\\{escaped}` is not an escape this subset reads; `\\\\` and `\\\"` alone")
                return None
            unescaped.append(escaped)
            index += 2
            continue
        unescaped.append(character)
        index += 1
    return Scalar("".join(unescaped), quoted=True)


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


def parse_flow_list(raw_value: str, line_number: int, errors: List[str]) -> Optional[List[Scalar]]:
    """`[a, b]` of plain, non-empty items each passing the plain-lexeme check in flow context; None after an error."""
    inner = raw_value.strip()[1:-1]
    if not inner.strip():
        return []
    items: List[Scalar] = []
    for item in inner.split(","):
        item = item.strip()
        if not item:
            errors.append(f"line {line_number}: an empty item in a flow list")
            return None
        if not check_plain_lexeme(item, line_number, errors, flow=True):
            return None
        items.append(Scalar(item))
    return items


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
            if is_omitted_value(raw_value):
                document.values[key] = Scalar("")  # omitted, unless the lines under it make it a map or a sequence
                current_key = key
            elif raw_value.strip().startswith("[") and raw_value.strip().endswith("]"):
                items = parse_flow_list(raw_value, line_number, document.errors)
                document.values[key] = items if items is not None else []
            else:
                scalar = parse_scalar(raw_value, line_number, document.errors)
                document.values[key] = scalar if scalar is not None else Scalar("")
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
            if existing == Scalar(""):
                existing = {}
                document.values[current_key] = existing
            if not isinstance(existing, dict):
                document.errors.append(f"line {line_number}: `{current_key}` mixes a map and a sequence")
            else:
                map_key = nested_key.group("key")
                if map_key in existing:
                    document.errors.append(f"line {line_number}: `{current_key}.{map_key}` is given again")
                # An omitted map value is kept as one; a line indented deeper under it is the indentation error.
                scalar = parse_scalar(nested_key.group("value") or "", line_number, document.errors)
                existing[map_key] = scalar if scalar is not None else Scalar("")
        else:
            if existing == Scalar(""):
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
