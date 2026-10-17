# SPDX-License-Identifier: MIT
"""The inventory half of verifying a signed set: `SHA256SUMS` describes the tree, and the tree is what it describes.

ai-tools-base's `assets-verify.lib.sh` makes the same check before it links a set, and this is its twin for a consumer
without base, a reviewer, or the publisher before shipping: `SHA256SUMS` lists every file of the set other than itself
and `SHA256SUMS.asc` exactly once, as `<sha256>  <relative path>`, and each hash matches. A line outside that shape,
a path outside the set or with a component outside the portable name set, a path listed twice, a file listed and
absent or present and unlisted, and a hash that differs are a MISMATCH. The walk reads the set by descriptor through
`safe_read`, from the directory descriptor the caller holds, as the validator's walk does: an entry the reader refuses
(a symbolic link, a special file, a file with a second hard link), an entry or a directory that cannot be opened or
listed, a file over the format's per-file bound, and a set over its file, directory, depth, entry or payload budget
leave the check UNABLE, naming the path. `attestations/` at the set root lies outside the inventory, so a later reader
can carry a third party's statement there without a change here.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import asset_format as fmt
from openpgp import MISMATCH, UNABLE, VERIFIED
from safe_read import RefusedRead, open_directory, read_file
from staged_build import INVENTORY_EXCLUDED, INVENTORY_FILE

INVENTORY_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<path>[^\n]+)$")


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
    lines = text.split("\n")
    for number, line in enumerate(lines, start=1):
        if line == "" and number == len(lines):
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


def verify_inventory(root_fd: int, inventory_text: str) -> InventoryCheck:
    """Hold the tree under the set directory `root_fd` to `inventory_text`, the text of its signed `SHA256SUMS`.

    The caller opened `root_fd` and closes it; the walk opens every directory under it without following a link.
    """
    check = InventoryCheck()
    listed = parse_inventory(inventory_text, check)
    check.listed = listed
    walk = _InventoryWalk(check)
    if not walk.run(root_fd):
        return check
    for relative in sorted(set(listed) - set(walk.found)):
        if not walk.was_refused(relative):
            check.mismatch(relative, "is listed and absent")
    for relative in sorted(set(walk.found) - set(listed)):
        check.mismatch(relative, "is present and not listed")
    for relative in sorted(set(walk.found) & set(listed)):
        if walk.found[relative] != listed[relative]:
            check.mismatch(relative, "does not match its hash in SHA256SUMS")
    return check


class _BudgetExceeded(Exception):
    """A format budget tripped at `path`; the walk stops there."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path


class _InventoryWalk:
    """One pass over the set: each regular file's digest in `found`, each path the reader refused in `refused`."""

    def __init__(self, check: InventoryCheck) -> None:
        self.check = check
        self.found: Dict[str, str] = {}
        self.refused: List[str] = []
        self.files = 0
        self.directories = 0
        self.bytes_read = 0

    def run(self, root_fd: int) -> bool:
        """Walk the tree under `root_fd`; False when a budget tripped and the walk stopped.

        An explicit stack of open directory descriptors replaces recursion, so depth is a budget and not a stack limit.
        """
        pending: List[Tuple[str, int, int]] = [("", root_fd, 0)]
        try:
            while pending:
                prefix, dir_fd, depth = pending.pop()
                try:
                    subdirectories = self.read_directory(prefix, dir_fd, depth)
                finally:
                    if dir_fd != root_fd:
                        os.close(dir_fd)
                pending.extend(reversed(subdirectories))
        except _BudgetExceeded as budget:
            self.check.unable(budget.path, str(budget))
            return False
        finally:
            for _, dir_fd, _ in pending:
                if dir_fd != root_fd:
                    os.close(dir_fd)
        return True

    def refuse(self, relative: str, message: str) -> None:
        self.check.unable(relative or ".", message)
        self.refused.append(relative)

    def was_refused(self, relative: str) -> bool:
        """True when `relative` or a directory holding it is a path the walk could not read."""
        return any(not refused or relative == refused or relative.startswith(refused + "/") for refused in self.refused)

    def read_directory(self, prefix: str, dir_fd: int, depth: int) -> List[Tuple[str, int, int]]:
        """Record every entry of one directory and return its subdirectories, each opened, for the walk to continue."""
        entries = []
        try:
            with os.scandir(dir_fd) as listing:
                for entry in listing:
                    entries.append(entry)
                    if len(entries) > fmt.SET_MAX_DIRECTORY_ENTRIES:
                        raise _BudgetExceeded(prefix or ".", f"holds more than {fmt.SET_MAX_DIRECTORY_ENTRIES} entries")
        except OSError as error:
            self.refuse(prefix, f"cannot be read: {error.strerror}")
            return []
        entries.sort(key=lambda entry: entry.name)
        subdirectories: List[Tuple[str, int, int]] = []
        try:
            for entry in entries:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if not prefix and entry.name in INVENTORY_EXCLUDED:
                    continue
                if not fmt.is_portable_file_name(entry.name):
                    self.check.mismatch(repr(relative), "is a name outside the portable name set, which no signed set carries")
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError as error:
                    self.refuse(relative, f"cannot be read: {error.strerror}")
                    continue
                if not is_directory:
                    self.record_file(relative, entry.name, dir_fd)
                elif prefix or entry.name != fmt.ATTESTATIONS_DIRECTORY:
                    child_fd = self.open_subdirectory(relative, entry.name, dir_fd, depth + 1)
                    if child_fd is not None:
                        subdirectories.append((relative, child_fd, depth + 1))
        except _BudgetExceeded:
            for _, child_fd, _ in subdirectories:
                os.close(child_fd)
            raise
        return subdirectories

    def open_subdirectory(self, relative: str, name: str, dir_fd: int, depth: int) -> Optional[int]:
        """The child directory's descriptor under the depth and directory budgets; None after a refusal."""
        if depth > fmt.SET_MAX_DEPTH:
            raise _BudgetExceeded(relative, f"is {depth} levels deep; a set is at most {fmt.SET_MAX_DEPTH}")
        self.directories += 1
        if self.directories > fmt.SET_MAX_DIRECTORIES:
            raise _BudgetExceeded(relative, f"the set holds more than {fmt.SET_MAX_DIRECTORIES} directories")
        try:
            return open_directory(name, dir_fd)
        except RefusedRead as refusal:
            self.refuse(relative, refusal.message)
        except OSError as error:
            self.refuse(relative, f"cannot be read: {error.strerror}")
        return None

    def record_file(self, relative: str, name: str, dir_fd: int) -> None:
        """Read one entry through the reader and keep its digest, counting it against the budgets."""
        self.files += 1
        if self.files > fmt.SET_MAX_FILES:
            raise _BudgetExceeded(relative, f"the set holds more than {fmt.SET_MAX_FILES} files")
        try:
            read = read_file(name, dir_fd, fmt.FILE_MAX_BYTES)
        except RefusedRead as refusal:
            self.refuse(relative, refusal.message)
            return
        except OSError as error:
            self.refuse(relative, f"cannot be read: {error.strerror}")
            return
        # The bytes a truncated read did read count too, so a tree of many over-size files trips the payload budget.
        self.bytes_read += len(read.data)
        if self.bytes_read > fmt.SET_MAX_BYTES:
            raise _BudgetExceeded(relative, f"the set holds more than {fmt.SET_MAX_BYTES} bytes")
        if read.truncated:
            self.refuse(relative, f"is {read.size} bytes, over the {fmt.FILE_MAX_BYTES} a file of a set holds")
            return
        self.found[relative] = read.digest
