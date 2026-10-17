# SPDX-License-Identifier: MIT
"""The inventory half of verifying a signed set: `SHA256SUMS` describes the tree, and the tree is what it describes.

ai-tools-base's `assets-verify.lib.sh` makes the same check before it links a set, and this is its twin for a consumer
without base, a reviewer, or the publisher before shipping: `SHA256SUMS` lists every file of the set other than itself
and `SHA256SUMS.asc` exactly once, as `<sha256>  <relative path>`, and each hash matches. A line outside that shape,
a path outside the set or with a component outside the portable name set, a path listed twice, a file listed and
absent, present and unlisted, or not a regular file, and a hash that differs are a MISMATCH; a directory the walk
cannot read, a file over the format's per-file bound and a set over its file-count or payload bound leave the check
UNABLE. The walk does not follow a link: a link is an entry that is not a regular file. `attestations/` at the set root
lies outside the inventory, so a later reader can carry a third party's statement there without a change here.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import stat
from dataclasses import dataclass, field
from typing import Dict, List

import asset_format as fmt
from staged_build import INVENTORY_EXCLUDED, INVENTORY_FILE

INVENTORY_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<path>[^\n]+)$")
ATTESTATIONS_DIRECTORY = "attestations"
VERIFIED = 0
MISMATCH = 1
UNABLE = 2


@dataclass
class InventoryCheck:
    """The outcome, VERIFIED, MISMATCH or UNABLE, and one (path, message) per problem found."""

    outcome: int = VERIFIED
    problems: List[tuple] = field(default_factory=list)
    listed: Dict[str, str] = field(default_factory=dict)

    def mismatch(self, path: str, message: str) -> None:
        if self.outcome != UNABLE:
            self.outcome = MISMATCH
        self.problems.append((path, message))

    def unable(self, path: str, message: str) -> None:
        self.outcome = UNABLE
        self.problems.append((path, message))


def parse_inventory(text: str, check: InventoryCheck) -> Dict[str, str]:
    """Each listed path with its digest; a line outside the shape, an unportable or escaping path and a path listed
    twice are mismatches."""
    listed: Dict[str, str] = {}
    for number, line in enumerate(text.split("\n"), start=1):
        if line == "" and number == len(text.split("\n")):
            continue
        match = INVENTORY_LINE.match(line)
        if match is None:
            check.mismatch(INVENTORY_FILE, f"line {number} is not `<sha256>  <path>`")
            continue
        path = match.group("path")
        parts = path.split("/")
        if path.startswith("/") or any(not fmt.is_portable_file_name(part) for part in parts):
            check.mismatch(INVENTORY_FILE, f"line {number} names `{path!r}`, a path outside the set or outside the portable name set")
            continue
        if path in listed:
            check.mismatch(INVENTORY_FILE, f"`{path}` is listed twice")
            continue
        listed[path] = match.group("digest")
    return listed


def verify_inventory(set_directory: pathlib.Path, inventory_text: str) -> InventoryCheck:
    """Hold the tree under `set_directory` to `inventory_text`, the bytes of its signed `SHA256SUMS`."""
    check = InventoryCheck()
    listed = parse_inventory(inventory_text, check)
    check.listed = listed
    found: Dict[str, str] = {}
    total_bytes = 0

    def walk_error(error: OSError) -> None:
        check.unable(str(error.filename or set_directory), f"cannot be read: {error.strerror}")

    for directory, directories, files in os.walk(set_directory, onerror=walk_error, followlinks=False):
        relative_directory = pathlib.Path(directory).relative_to(set_directory)
        if relative_directory == pathlib.Path("."):
            directories[:] = [name for name in directories if name != ATTESTATIONS_DIRECTORY]
        for name in sorted(files) + [name for name in directories if os.path.islink(os.path.join(directory, name))]:
            relative = (relative_directory / name).as_posix()
            if relative in INVENTORY_EXCLUDED:
                continue
            if any(not fmt.is_portable_file_name(part) for part in relative.split("/")):
                check.mismatch(repr(relative), "is a name outside the portable name set, which no signed set carries")
                continue
            if len(found) + 1 > fmt.SET_MAX_FILES:
                check.unable(relative, f"the set holds more than {fmt.SET_MAX_FILES} files")
                return check
            path = os.path.join(directory, name)
            status = os.lstat(path)
            if not stat.S_ISREG(status.st_mode):
                found[relative] = ""
                if relative in listed:
                    check.mismatch(relative, "is listed and is not a regular file")
                continue
            if status.st_size > fmt.FILE_MAX_BYTES:
                check.unable(relative, f"is {status.st_size} bytes, over the {fmt.FILE_MAX_BYTES} a file of a set holds")
                continue
            total_bytes += status.st_size
            if total_bytes > fmt.SET_MAX_BYTES:
                check.unable(relative, f"the set holds more than {fmt.SET_MAX_BYTES} bytes")
                return check
            digest = hashlib.sha256()
            with open(path, "rb", opener=lambda opened, flags: os.open(opened, flags | os.O_NOFOLLOW)) as handle:
                for chunk in iter(lambda: handle.read(1 << 16), b""):
                    digest.update(chunk)
            found[relative] = digest.hexdigest()
    for relative in sorted(set(listed) - set(found)):
        check.mismatch(relative, "is listed and absent")
    for relative in sorted(set(found) - set(listed)):
        check.mismatch(relative, "is present and not listed")
    for relative in sorted(set(found) & set(listed)):
        if found[relative] and found[relative] != listed[relative]:
            check.mismatch(relative, "does not match its hash in SHA256SUMS")
    return check
