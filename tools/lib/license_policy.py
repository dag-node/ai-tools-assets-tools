# SPDX-License-Identifier: MIT
"""The SPDX allowlist in force for a repository, and the checks that hold a licence declaration to it.

The default list is the GPLv3-compatible permissive set; `publisher.conf` `licenses=[...]` replaces it, an invalid
list refuses every licence, and an explicitly empty list refuses every licence too, which is the less-access reading
of a list a publisher wrote and got wrong. Three places declare a licence for set content -- `set.conf`, a skill's
`license` field and a vendored asset's `UPSTREAM.conf` -- and each is held to the same list; `UPSTREAM.conf` records
upstream terms and does not exempt the asset. A file outside a set is read for its own SPDX header, with the
repository's `REUSE.toml` annotations as the fallback REUSE applies, so `check-licenses` judges the whole tree.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePath
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from asset_format import DEFAULT_LICENSE_ALLOWLIST, FILE_MAX_BYTES, LICENSE_TEXTS_DIRECTORY, SPDX_HEADER, SPDX_HEADER_LINES_READ
from findings import FindingCollector
from key_value_config import ConfigDocument
from safe_read import RefusedRead, read_file_under, read_text_under
from spdx_expression import SpdxEvaluation, evaluate_expression

REUSE_TABLE_HEADER = re.compile(r"^\s*\[\[annotations\]\]\s*$")
REUSE_KEY_VALUE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*(?P<value>.+?)\s*$")
# A licence header sits in a file's first lines; this many bytes hold them, and a longer file is read no further.
SPDX_HEADER_BYTES_READ = 64 * 1024


def allowlist_in_force(publisher: Optional[ConfigDocument]) -> Tuple[Tuple[str, ...], Optional[str]]:
    """The identifiers in force, and the reason the publisher's list is invalid where it is (the list is then empty)."""
    if publisher is None or not publisher.has("licenses"):
        return DEFAULT_LICENSE_ALLOWLIST, None
    items, reason = publisher.list_value("licenses")
    if reason is not None:
        return (), f"publisher.conf `licenses` is not a list ({reason}); every licence is refused until it is fixed"
    return tuple(items), None


def check_declared_license(collector: FindingCollector, path: str, expression: str, allowlist: Sequence[str],
                           what: str) -> SpdxEvaluation:
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


def file_spdx_expression(root_fd: int, relative_path: PurePath) -> Optional[str]:
    """The expression of an SPDX licence header in the first lines of the file under `root_fd`, or None.

    A file that is not a regular file, or cannot be read, has no header of its own and falls to the annotations.
    """
    try:
        read = read_file_under(root_fd, relative_path, SPDX_HEADER_BYTES_READ)
    except (RefusedRead, OSError):
        return None
    for line in read.data.decode("utf-8", errors="replace").splitlines()[:SPDX_HEADER_LINES_READ]:
        match = SPDX_HEADER.search(line)
        if match:
            return match.group("expression").strip()
    return None


def read_reuse_annotations(root_fd: int) -> List[Tuple[str, str]]:
    """The `(path glob, SPDX-License-Identifier)` of each `[[annotations]]` table in the REUSE.toml under `root_fd`,
    in file order; an empty list when the repository has none.

    A bounded reader of the TOML the project writes: one key per line, a string or an array of strings. REUSE applies
    the last matching annotation, and a file's own header takes priority under `precedence = "closest"`.
    """
    try:
        text = read_text_under(root_fd, PurePath("REUSE.toml"), FILE_MAX_BYTES)
    except FileNotFoundError:
        return []
    annotations: List[Tuple[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in text.splitlines():
        if REUSE_TABLE_HEADER.match(line):
            if current is not None:
                annotations.extend(_annotation_rows(current))
            current = {}
            continue
        match = REUSE_KEY_VALUE.match(line)
        if match and current is not None and not line.lstrip().startswith("#"):
            current[match.group("key")] = match.group("value")
    if current is not None:
        annotations.extend(_annotation_rows(current))
    return annotations


def _annotation_rows(table: Dict[str, str]) -> List[Tuple[str, str]]:
    paths = _toml_strings(table.get("path", ""))
    expression = _toml_strings(table.get("SPDX-License-Identifier", ""))
    if not paths or not expression:
        return []
    return [(glob, expression[0]) for glob in paths]


def _toml_strings(raw: str) -> List[str]:
    raw = raw.strip()
    if raw.startswith("["):
        raw = raw.strip("[]")
        return [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]
    return [raw.strip("\"'")] if raw else []


def annotation_for(relative_path: str, annotations: Sequence[Tuple[str, str]]) -> Optional[str]:
    """The expression of the last annotation whose glob matches `relative_path`."""
    expression: Optional[str] = None
    for glob, candidate in annotations:
        if fnmatch.fnmatchcase(relative_path, glob) or fnmatch.fnmatchcase(relative_path, glob.replace("**/", "")):
            expression = candidate
    return expression


def exception_for(relative_path: str, exceptions: Sequence[Tuple[str, str]]) -> Optional[str]:
    """The identifier an `--exception GLOB=ID` admits for `relative_path`, or None."""
    for glob, identifier in exceptions:
        if fnmatch.fnmatchcase(relative_path, glob):
            return identifier
    return None
