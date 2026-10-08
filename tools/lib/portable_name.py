# SPDX-License-Identifier: MIT
"""The portable name the `file.name` finding proposes for a file name outside the portable set.

The proposal depends on the name alone, so every host proposes the same one: NFKD splits a composed letter into its
base letter and combining marks, the marks are dropped, each run of characters outside the portable set becomes one
`_`, a leading `-` becomes `_`, and the result is cut to the byte bound. Where no letter or digit remains, the proposal
is `renamed-` and the first 16 hex digits of the SHA-256 of the name's bytes. The validator prints the proposal and
does not rename the file, since a rename also owes every link to the file a rewrite.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

import asset_format as fmt

OUTSIDE_PORTABLE_SET = re.compile(r"[^A-Za-z0-9._-]+")
UNDERSCORE_RUN = re.compile(r"_{2,}")
LETTER_OR_DIGIT = re.compile(r"[A-Za-z0-9]")


def propose_portable_name(name: str) -> str:
    """A name `asset_format.is_portable_file_name` accepts, derived from `name`; a walk's undecodable bytes arrive
    as surrogate escapes and are replaced like any other character outside the set."""
    folded = "".join(character for character in unicodedata.normalize("NFKD", name) if not unicodedata.combining(character))
    proposal = OUTSIDE_PORTABLE_SET.sub("_", folded)
    if proposal.startswith("-"):
        proposal = "_" + proposal[1:]
    proposal = UNDERSCORE_RUN.sub("_", proposal)[:fmt.PORTABLE_FILE_NAME_MAX_BYTES]
    if not LETTER_OR_DIGIT.search(proposal):
        digest = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()
        proposal = f"renamed-{digest[:16]}"
    return proposal
