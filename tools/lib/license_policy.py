# SPDX-License-Identifier: MIT
"""The SPDX allowlist in force for a repository, and the checks that hold a licence declaration to it.

The default list is the GPLv3-compatible permissive set; `publisher.conf` `licenses=[...]` replaces it, an invalid
list refuses every licence, and an explicitly empty list refuses every licence too, which is the less-access reading
of a list a publisher wrote and got wrong. Three places declare a licence for set content -- `set.conf`, a skill's
`license` field and a vendored asset's `UPSTREAM.conf` -- and each is held to the same list; `UPSTREAM.conf` records
upstream terms and does not exempt the asset.

A file outside a set is judged as REUSE 3.2 resolves it, and every expression that applies is held to the list, since
REUSE combines them: the file's own information is each `SPDX-License-Identifier` header anywhere in the file, read
whole under the file cap and outside a `REUSE-IgnoreStart`/`REUSE-IgnoreEnd` block, or the headers of its
`<file>.license` sidecar where one exists; a file over the cap is refused rather than judged on a prefix; the
repository's `REUSE.toml` supplies annotations whose `path` globs are matched with REUSE's grammar (`*` and `?` stop
at `/`, a `**` segment crosses it, `\\` escapes a metacharacter) by a segment-wise wildcard match whose work is bounded
by the glob's and the path's lengths, the last matching annotation applies -- whether or not it declares a licence --
and its `precedence` decides the combination: `closest` (the default) takes the file's own information where it has
any, `aggregate` takes the file's own and the annotation's, `override` takes the annotation alone. The `REUSE.toml`
reader parses a bounded TOML subset and refuses the file whole on a line outside it, so no file is judged on an
annotation it may have misread; `reuse lint` in CI is the check that the file is TOML. A `REUSE.toml` inside a
subdirectory governs the files under it with rules this check does not read, so each of those files is refused rather
than judged by the root's annotations.
"""
from __future__ import annotations

import functools
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from asset_format import DEFAULT_LICENSE_ALLOWLIST, FILE_MAX_BYTES, LICENSE_TEXTS_DIRECTORY, REUSE_GLOB_MAX_LENGTH, SPDX_HEADER
from findings import FindingCollector
from key_value_config import KeyValueDocument
from safe_read import RefusedRead, read_file_under, read_text_under
from spdx_expression import SpdxValidationResult, evaluate_expression

REUSE_TABLE_HEADER = re.compile(r"^\s*\[\[annotations\]\]\s*$")
REUSE_KEY_VALUE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*(?P<value>.*?)\s*$")
REUSE_PRECEDENCES: Tuple[str, ...] = ("closest", "aggregate", "override")
REUSE_PRECEDENCE_DEFAULT = "closest"
LICENSE_SIDECAR_SUFFIX = ".license"
# REUSE's ignore-block markers, spelled in two halves so that neither this reader nor `reuse lint` opens a block on
# this line.
REUSE_IGNORE_START = "REUSE-Ignore" + "Start"
REUSE_IGNORE_END = "REUSE-Ignore" + "End"


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
    is `LICENSES/<identifier>.txt` under none of `text_directories`, the operator-named directories outside it.

    An external text counts only as one regular file with one link, `lstat`ed so a link at the name is not followed:
    `build-set` reads it through `safe_read` under the same rule, so what validation accepts is what a build can carry.
    """
    for identifier in sorted(set(identifiers) - set(carried)):
        if not any(_is_one_regular_file(directory / LICENSE_TEXTS_DIRECTORY / f"{identifier}.txt") for directory in text_directories):
            searched = ", ".join(str(directory / LICENSE_TEXTS_DIRECTORY) for directory in [Path("."), *text_directories])
            collector.refuse(declaring_path, "license.text", f"no `{identifier}.txt` (one regular file, not a link) under {searched}")


def _is_one_regular_file(path: Path) -> bool:
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and status.st_nlink == 1


def file_spdx_expressions(root_fd: int, relative_path: PurePath) -> List[str]:
    """Every expression of an SPDX licence header anywhere in the file under `root_fd`, in file order, the lines
    from a `REUSE-IgnoreStart` marker to the next `REUSE-IgnoreEnd` (or the file's end) left out, as REUSE reads them.

    The file is read whole up to `FILE_MAX_BYTES`; one over that raises RefusedRead(`license.file`), since a prefix
    cannot show that no later header declares another licence. A file that is not a regular file, or cannot be read,
    has no header of its own and falls to the annotations.
    """
    try:
        read = read_file_under(root_fd, relative_path, FILE_MAX_BYTES)
    except (RefusedRead, OSError):
        return []
    if read.truncated:
        raise RefusedRead("license.file", f"is {read.size} bytes, over the {FILE_MAX_BYTES} this check reads whole; a header past the bound would go unread")
    expressions: List[str] = []
    ignoring = False
    for line in read.data.decode("utf-8", errors="replace").splitlines():
        if ignoring:
            ignoring = REUSE_IGNORE_END not in line
            continue
        if REUSE_IGNORE_START in line:
            ignoring = True
            continue
        match = SPDX_HEADER.search(line)
        if match:
            expressions.append(match.group("expression").strip())
    return expressions


TomlValue = Union[str, Tuple[str, ...]]
TOML_VALUE_FORMS = "a value is a \"basic\" or 'literal' string on one line, or a one-line array of them"


def read_reuse_annotations(root_fd: int) -> Tuple[List[ReuseAnnotation], Optional[str]]:
    """The `[[annotations]]` tables of the REUSE.toml under `root_fd`, in file order, and what is wrong with the file
    where something is; an empty list when the repository has none.

    A bounded reader of a TOML subset that refuses what it does not parse rather than misreading it. Inside a table a
    line is blank, a `#` comment, or `key = value` with a bare key and the value a `"basic"` string (`\\\\` and `\\"`
    escapes alone), a `'literal'` string, or a one-line array of such strings. Text after a value (a `#` comment
    included), a multi-line array or string, an inline table, a bare value, a dotted or quoted key, a table header
    other than `[[annotations]]`, a key given twice in one table, a table without `path`, and a `precedence` outside
    REUSE's three refuse the file whole, so the caller does not judge any file on a reading it cannot vouch for. The
    lines before the first table (`version`, the package keys) are not read. An annotation that does not declare a
    licence is kept with no expressions, since REUSE resolves the last matching table whether or not it names one.
    """
    try:
        text = read_text_under(root_fd, PurePath("REUSE.toml"), FILE_MAX_BYTES)
    except FileNotFoundError:
        return [], None
    except RefusedRead as refusal:
        return [], refusal.message
    tables: List[Dict[str, TomlValue]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if REUSE_TABLE_HEADER.match(line):
            tables.append({})
            continue
        if stripped.startswith("["):
            return [], f"line {line_number}: `{stripped}` is a table header this reader does not read; it reads `[[annotations]]` tables alone"
        if not tables:
            continue
        match = REUSE_KEY_VALUE.match(line)
        if not match:
            return [], f"line {line_number}: `{stripped}` is not `key = value` with a bare key"
        key = match.group("key")
        if key in tables[-1]:
            return [], f"line {line_number}: `{key}` is given again in one table"
        value, problem = _toml_value(match.group("value"))
        if problem is not None:
            return [], f"line {line_number}: `{key} =`: {problem}"
        tables[-1][key] = value
    annotations: List[ReuseAnnotation] = []
    for index, table in enumerate(tables, start=1):
        path = table.get("path")
        if path is None:
            return [], f"annotations table {index} has no `path`"
        globs = (path,) if isinstance(path, str) else path
        if not globs:
            return [], f"annotations table {index}: `path` is an empty array"
        declared = table.get("SPDX-License-Identifier", ())
        expressions = (declared,) if isinstance(declared, str) else declared
        precedence = table.get("precedence", REUSE_PRECEDENCE_DEFAULT)
        if not isinstance(precedence, str) or precedence not in REUSE_PRECEDENCES:
            return [], f"`precedence = {precedence}` is not one of " + ", ".join(REUSE_PRECEDENCES)
        for glob in globs:
            problem = reuse_glob_problem(glob)
            if problem is not None:
                return [], f"annotations table {index}: path `{glob}` {problem}"
        annotations.append(ReuseAnnotation(tuple(globs), tuple(expressions), precedence))
    return annotations, None


def _toml_value(raw: str) -> Tuple[TomlValue, Optional[str]]:
    """The string, or the tuple of strings, a one-line TOML value holds; the problem where it is outside the subset."""
    if not raw.startswith("["):
        value, rest, problem = _toml_string(raw)
        if problem is None and rest.strip():
            problem = f"`{rest.strip()}` follows the value; nothing follows a value in this subset, a comment included"
        return value, problem
    items: List[str] = []
    rest = raw[1:].lstrip()
    while not rest.startswith("]"):
        if not rest:
            return (), "the array does not close on its line"
        item, rest, problem = _toml_string(rest)
        if problem is not None:
            return (), problem
        items.append(item)
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
        elif not rest.startswith("]"):
            return (), "an array holds quoted strings separated by commas"
    rest = rest[1:]
    if rest.strip():
        return (), f"`{rest.strip()}` follows the value; nothing follows a value in this subset, a comment included"
    return tuple(items), None


def _toml_string(text: str) -> Tuple[str, str, Optional[str]]:
    """One quoted string at the start of `text`: its value, the text after its closing quote, and the problem."""
    if not text or text[0] not in ("\"", "'"):
        return "", text, TOML_VALUE_FORMS
    quote = text[0]
    if text.startswith(quote * 3):
        return "", text, "a multi-line string is outside the subset"
    resolved: List[str] = []
    index = 1
    while index < len(text):
        character = text[index]
        if character == quote:
            return "".join(resolved), text[index + 1:], None
        if quote == "\"" and character == "\\":
            escaped = text[index + 1: index + 2]
            if escaped not in ("\\", "\""):
                return "", text, f"the escape `\\{escaped}` is outside the subset; `\\\\` and `\\\"` alone"
            resolved.append(escaped)
            index += 2
            continue
        resolved.append(character)
        index += 1
    return "", text, "the string does not close on its line"


Token = Tuple[str, str]  # (`lit`, the character), (`star`, ``) or (`qmark`, ``)
Segment = Optional[Tuple[Token, ...]]  # None is a `**` segment


@functools.lru_cache(maxsize=256)
def compile_reuse_glob(glob: str) -> Tuple[Segment, ...]:
    """REUSE's glob split on `/` into segments of tokens: `*` and `?` stop at `/`, a segment that is exactly `**`
    spans directories, and `\\` makes the character after it a literal.

    Raises ValueError for a glob this reader does not read: one over `REUSE_GLOB_MAX_LENGTH`, or with `**` beside
    another character in a segment, whose meaning differs between readers.
    """
    if len(glob) > REUSE_GLOB_MAX_LENGTH:
        raise ValueError(f"is {len(glob)} characters; a glob is at most {REUSE_GLOB_MAX_LENGTH}")
    segments: List[Segment] = []
    tokens: List[Token] = []
    index = 0
    while index <= len(glob):
        character = glob[index] if index < len(glob) else "/"
        if character == "/":
            if tokens == [("star", ""), ("star", "")]:
                segments.append(None)
            elif any(first[0] == "star" == second[0] for first, second in zip(tokens, tokens[1:])):
                raise ValueError("carries `**` beside another character; `**` stands alone between slashes in this reader")
            else:
                segments.append(tuple(tokens))
            tokens = []
        elif character == "\\" and index + 1 < len(glob):
            index += 1
            tokens.append(("lit", glob[index]))
        elif character == "*":
            tokens.append(("star", ""))
        elif character == "?":
            tokens.append(("qmark", ""))
        else:
            tokens.append(("lit", character))
        index += 1
    return tuple(segments)


def reuse_glob_problem(glob: str) -> Optional[str]:
    """What keeps the glob outside this reader's grammar, or None for one it reads."""
    try:
        compile_reuse_glob(glob)
    except ValueError as problem:
        return str(problem)
    return None


def _segment_matches(tokens: Tuple[Token, ...], text: str) -> bool:
    """The two-pointer wildcard match of one segment against one path part, with one backtrack point per `*`."""
    token_index = text_index = 0
    star_token, star_text = -1, 0
    while text_index < len(text):
        if token_index < len(tokens) and tokens[token_index][0] == "star":
            star_token, star_text = token_index, text_index
            token_index += 1
        elif token_index < len(tokens) and (tokens[token_index][0] == "qmark" or tokens[token_index] == ("lit", text[text_index])):
            token_index += 1
            text_index += 1
        elif star_token >= 0:
            star_text += 1
            token_index, text_index = star_token + 1, star_text
        else:
            return False
    return all(token[0] == "star" for token in tokens[token_index:])


def matches_reuse_glob(relative_path: str, glob: str) -> bool:
    """True when the glob matches the `/`-joined relative path; `dir/**` is what lies inside `dir`, `**/` matches
    zero directories too. The work is bounded by the segment and part counts, whatever the glob."""
    segments = compile_reuse_glob(glob)
    parts = relative_path.split("/")
    # matches[g][p]: segments g.. match parts p..; a `**` segment takes zero or more parts, one or more when it ends
    # the glob (`dir/**` does not match `dir`).
    matches = [[False] * (len(parts) + 1) for _ in range(len(segments) + 1)]
    matches[len(segments)][len(parts)] = True
    for g in range(len(segments) - 1, -1, -1):
        for p in range(len(parts), -1, -1):
            if segments[g] is None:
                matches[g][p] = p < len(parts) if g == len(segments) - 1 else any(matches[g + 1][p:])
            else:
                matches[g][p] = p < len(parts) and _segment_matches(segments[g], parts[p]) and matches[g + 1][p + 1]
    return matches[0][0]


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
