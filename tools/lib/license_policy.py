# SPDX-License-Identifier: MIT
"""The SPDX allowlist in force for a repository, and the checks that hold a licence declaration to it.

The default list is the GPLv3-compatible permissive set; `publisher.conf` `licenses=[...]` replaces it, an invalid
list refuses every licence, and an explicitly empty list refuses every licence too, which is the less-access reading
of a list a publisher wrote and got wrong. Three places declare a licence for set content -- `set.conf`, a skill's
`license` field and a vendored asset's `UPSTREAM.conf` -- and each is held to the same list; `UPSTREAM.conf` records
upstream terms and does not exempt the asset.

A file outside a set is judged as REUSE 3.2 resolves it, and every expression that applies is held to the list, since
REUSE combines them: the file's own information is each `SPDX-License-Identifier` header in its first lines, or the
headers of its `<file>.license` sidecar where one exists; the repository's `REUSE.toml` supplies annotations whose
`path` globs are matched with REUSE's grammar (`*` and `?` stop at `/`, `**` crosses it, `\\` escapes a metacharacter),
the last matching annotation applies, and its `precedence` decides the combination -- `closest` (the default) takes
the file's own information where it has any, `aggregate` takes both, `override` takes the annotation alone. A
`REUSE.toml` inside a subdirectory governs the files under it with rules this check does not read, so each of those
files is refused rather than judged by the root's annotations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from asset_format import DEFAULT_LICENSE_ALLOWLIST, FILE_MAX_BYTES, LICENSE_TEXTS_DIRECTORY, SPDX_HEADER, SPDX_HEADER_LINES_READ
from findings import FindingCollector
from key_value_config import KeyValueDocument
from safe_read import RefusedRead, read_file_under, read_text_under
from spdx_expression import SpdxValidationResult, evaluate_expression

REUSE_TABLE_HEADER = re.compile(r"^\s*\[\[annotations\]\]\s*$")
REUSE_KEY_VALUE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*(?P<value>.+?)\s*$")
REUSE_PRECEDENCES: Tuple[str, ...] = ("closest", "aggregate", "override")
REUSE_PRECEDENCE_DEFAULT = "closest"
LICENSE_SIDECAR_SUFFIX = ".license"
# A licence header sits in a file's first lines; this many bytes hold them, and a longer file is read no further.
SPDX_HEADER_BYTES_READ = 64 * 1024


@dataclass(frozen=True)
class ReuseAnnotation:
    """One `[[annotations]]` table: its path globs, the expressions it declares, and how it combines with a header."""

    globs: Tuple[str, ...]
    expressions: Tuple[str, ...]
    precedence: str = REUSE_PRECEDENCE_DEFAULT


def allowlist_in_force(publisher: Optional[KeyValueDocument]) -> Tuple[Tuple[str, ...], Optional[str]]:
    """The identifiers in force, and the reason the publisher's list is invalid where it is (the list is then empty)."""
    if publisher is None or not publisher.has("licenses"):
        return DEFAULT_LICENSE_ALLOWLIST, None
    items, reason = publisher.list_value("licenses")
    if reason is not None:
        return (), f"publisher.conf `licenses` is not a list ({reason}); every licence is refused until it is fixed"
    return tuple(items), None


def check_declared_license(collector: FindingCollector, path: str, expression: str, allowlist: Sequence[str],
                           what: str) -> SpdxValidationResult:
    """Hold one declared expression to the list, reporting a malformed expression apart from a policy refusal."""
    evaluation = evaluate_expression(expression, allowlist)
    if evaluation.syntax_error is not None:
        collector.refuse(path, "license.expression", f"{what} `{expression}`: {evaluation.syntax_error}")
    for reason in evaluation.unsupported:
        collector.refuse(path, "license.expression", f"{what}: {reason}")
    if evaluation.is_well_formed and evaluation.outside_allowlist:
        outside = ", ".join(f"`{identifier}`" for identifier in evaluation.outside_allowlist)
        in_force = ", ".join(allowlist) if allowlist else "(empty)"
        collector.refuse(path, "license.allowlist", f"{what} `{expression}` names {outside}, outside the list in force: {in_force}")
    return evaluation


def check_license_texts(collector: FindingCollector, declaring_path: str, identifiers: Iterable[str], carried: Set[str],
                        text_directories: Sequence[Path]) -> None:
    """Report an identifier whose text is not among `carried` (the texts a walk read inside the tree under check) and
    is `LICENSES/<identifier>.txt` under none of `text_directories`, the operator-named directories outside it."""
    for identifier in sorted(set(identifiers) - set(carried)):
        if not any((directory / LICENSE_TEXTS_DIRECTORY / f"{identifier}.txt").is_file() for directory in text_directories):
            searched = ", ".join(str(directory / LICENSE_TEXTS_DIRECTORY) for directory in [Path("."), *text_directories])
            collector.refuse(declaring_path, "license.text", f"no `{identifier}.txt` under {searched}")


def file_spdx_expressions(root_fd: int, relative_path: PurePath) -> List[str]:
    """Every expression of an SPDX licence header in the first lines of the file under `root_fd`, in file order.

    A file that is not a regular file, or cannot be read, has no header of its own and falls to the annotations.
    """
    try:
        read = read_file_under(root_fd, relative_path, SPDX_HEADER_BYTES_READ)
    except (RefusedRead, OSError):
        return []
    expressions: List[str] = []
    for line in read.data.decode("utf-8", errors="replace").splitlines()[:SPDX_HEADER_LINES_READ]:
        match = SPDX_HEADER.search(line)
        if match:
            expressions.append(match.group("expression").strip())
    return expressions


def read_reuse_annotations(root_fd: int) -> Tuple[List[ReuseAnnotation], Optional[str]]:
    """The `[[annotations]]` tables of the REUSE.toml under `root_fd`, in file order, and what is wrong with the file
    where something is; an empty list when the repository has none.

    A bounded reader of the TOML the project writes: one key per line, a string or an array of strings.
    """
    try:
        text = read_text_under(root_fd, PurePath("REUSE.toml"), FILE_MAX_BYTES)
    except FileNotFoundError:
        return [], None
    except RefusedRead as refusal:
        return [], refusal.message
    tables: List[Dict[str, str]] = []
    for line in text.splitlines():
        if REUSE_TABLE_HEADER.match(line):
            tables.append({})
            continue
        match = REUSE_KEY_VALUE.match(line)
        if match and tables and not line.lstrip().startswith("#"):
            tables[-1][match.group("key")] = match.group("value")
    annotations: List[ReuseAnnotation] = []
    for table in tables:
        globs = tuple(_toml_strings(table.get("path", "")))
        expressions = tuple(_toml_strings(table.get("SPDX-License-Identifier", "")))
        precedence = _toml_strings(table.get("precedence", "")) or [REUSE_PRECEDENCE_DEFAULT]
        if precedence[0] not in REUSE_PRECEDENCES:
            return [], f"`precedence = {precedence[0]}` is not one of " + ", ".join(REUSE_PRECEDENCES)
        if globs and expressions:
            annotations.append(ReuseAnnotation(globs, expressions, precedence[0]))
    return annotations, None


def _toml_strings(raw: str) -> List[str]:
    """The string or the array of strings a TOML value holds; a basic string's `\\\\` and `\\"` escapes are resolved."""
    raw = raw.strip()
    items = [item.strip() for item in raw.strip("[]").split(",") if item.strip()] if raw.startswith("[") else ([raw] if raw else [])
    strings: List[str] = []
    for item in items:
        if item.startswith('"') and item.endswith('"'):
            strings.append(re.sub(r'\\([\\"])', r"\1", item[1:-1]))
        else:
            strings.append(item.strip("'"))
    return strings


def reuse_glob_regex(glob: str) -> "re.Pattern[str]":
    """REUSE's glob as a regular expression over a `/`-joined relative path: `*` and `?` stop at `/`, `**` crosses
    it (`**/` matches zero directories too, `dir/**` everything inside `dir`), and `\\` escapes the character after it."""
    parts: List[str] = []
    index = 0
    while index < len(glob):
        character = glob[index]
        if character == "\\" and index + 1 < len(glob):
            parts.append(re.escape(glob[index + 1]))
            index += 2
        elif glob.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif glob.startswith("/**", index) and index + 3 == len(glob):
            parts.append("/.*")
            index += 3
        elif glob.startswith("**", index):
            parts.append(".*")
            index += 2
        elif character == "*":
            parts.append("[^/]*")
            index += 1
        elif character == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(character))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def matches_reuse_glob(relative_path: str, glob: str) -> bool:
    return bool(reuse_glob_regex(glob).match(relative_path))


def resolve_reuse_annotation(relative_path: str, annotations: Sequence[ReuseAnnotation]) -> Optional[ReuseAnnotation]:
    """The last annotation one of whose globs matches `relative_path`, as REUSE resolves it."""
    resolved: Optional[ReuseAnnotation] = None
    for annotation in annotations:
        if any(matches_reuse_glob(relative_path, glob) for glob in annotation.globs):
            resolved = annotation
    return resolved


def applicable_expressions(root_fd: int, relative_path: PurePath, tracked: Set[str], annotations: Sequence[ReuseAnnotation]) -> List[str]:
    """Every expression REUSE applies to the file: its own (the sidecar's headers where `<file>.license` is tracked,
    else its headers), combined with the resolved annotation's under that annotation's precedence."""
    sidecar = PurePath(str(relative_path) + LICENSE_SIDECAR_SUFFIX)
    own = file_spdx_expressions(root_fd, sidecar if str(sidecar) in tracked else relative_path)
    annotation = resolve_reuse_annotation(relative_path.as_posix(), annotations)
    if annotation is None:
        return own
    if annotation.precedence == "override":
        return list(annotation.expressions)
    if annotation.precedence == "aggregate":
        return own + list(annotation.expressions)
    return own or list(annotation.expressions)


def exception_for(relative_path: str, exceptions: Sequence[Tuple[str, str]]) -> Optional[str]:
    """The identifier an `--exception GLOB=ID` admits for `relative_path`, under the REUSE glob grammar, or None."""
    for glob, identifier in exceptions:
        if matches_reuse_glob(relative_path, glob):
            return identifier
    return None
