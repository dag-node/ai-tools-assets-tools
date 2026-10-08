# SPDX-License-Identifier: MIT
"""The links of a Markdown body that name a file by a relative path, for the `body.relative-link` rule.

A link is an inline link or image, `[text](target)`, or a reference definition, `[label]: target`; an angle-bracketed
target is read without its brackets. A target with a URI scheme (`https:`, `mailto:`) or opening with `#` does not name
a file and is not returned; every other target is returned with its fragment removed. The reader skips a fenced code
block and a code span, so a link an asset shows as code is not returned. It covers the link forms an asset writes,
a subset of CommonMark: an indented code block is read as text, a target holding a parenthesis ends at it, and an HTML
`<a>` element is not read.
"""
from __future__ import annotations

import re
from typing import Iterator, Sequence, Tuple

FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
CODE_SPAN = re.compile(r"(`+).+?\1")
INLINE_LINK = re.compile(r"\]\(\s*(<[^<>\n]*>|[^\s()<>]+)")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(<[^<>\n]*>|\S+)")
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def relative_link_targets(lines: Sequence[str], first_line_number: int = 1) -> Iterator[Tuple[int, str]]:
    """Each relative link target in `lines`, fragment removed, with the number of the line it stands on."""
    fence = ""
    for line_number, line in enumerate(lines, start=first_line_number):
        if fence:
            closer = FENCE_CLOSE.match(line)
            if closer and closer.group(1)[0] == fence[0] and len(closer.group(1)) >= len(fence):
                fence = ""
            continue
        opener = FENCE_OPEN.match(line)
        if opener:
            fence = opener.group(1)
            continue
        text = CODE_SPAN.sub("", line)
        targets = [match.group(1) for match in INLINE_LINK.finditer(text)]
        definition = REFERENCE_DEFINITION.match(text)
        if definition:
            targets.append(definition.group(1))
        for target in targets:
            if target.startswith("<"):
                target = target[1:-1].strip()
            if not target or target.startswith("#") or URI_SCHEME.match(target):
                continue
            yield line_number, target.split("#", 1)[0]
