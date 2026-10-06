# SPDX-License-Identifier: MIT
"""The writer every scaffolding and build command creates files through: no symbolic link on the path, no link followed
at the name, and a new file created exclusively.

A command that writes under a publisher repository walks a tree a pull request may have shaped, so a path's existing
components are `lstat`ed before a write and a symbolic link among them refuses (`file.symlink`); a directory is created
with `mkdir`, which does not follow a link at its name; a new file is opened with `O_CREAT|O_EXCL|O_NOFOLLOW`, so a
dangling link or any other entry at the name fails with EEXIST instead of being written through; and a generated file
that is rewritten in place is `lstat`ed as a regular file first and then opened with `O_TRUNC|O_NOFOLLOW`. Every
refusal is a `safe_read.RefusedRead`, so a command reports it under the rule the caller names.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PurePath

from safe_read import RefusedRead

CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
REPLACE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_MODE = 0o644
DIRECTORY_MODE = 0o755


def check_components(root: Path, relative_path: PurePath) -> None:
    """Refuse when an existing component of `relative_path` under `root` is a symbolic link or not a directory.

    The last component is not judged here: the write that follows decides what may stand at the name.
    """
    current = root
    for part in relative_path.parts[:-1]:
        current = current / part
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            return  # the rest is created by the write
        if stat.S_ISLNK(status.st_mode):
            raise RefusedRead("file.symlink", f"`{current.relative_to(root)}` is a symbolic link; a write does not go through one")
        if not stat.S_ISDIR(status.st_mode):
            raise RefusedRead("file.special", f"`{current.relative_to(root)}` is not a directory")


def make_directories(root: Path, relative_path: PurePath) -> None:
    """Create `relative_path` under `root` and the directories leading to it, after checking their components."""
    check_components(root, relative_path / "x")
    current = root
    for part in relative_path.parts:
        current = current / part
        try:
            os.mkdir(current, DIRECTORY_MODE)
        except FileExistsError:
            if stat.S_ISLNK(os.lstat(current).st_mode) or not current.is_dir():
                raise RefusedRead("file.symlink", f"`{current.relative_to(root)}` exists and is not a directory") from None


def create_file(root: Path, relative_path: PurePath, data: bytes) -> None:
    """Create the file exclusively under `root`, with its missing directories; an entry already at the name refuses."""
    check_components(root, relative_path)
    if relative_path.parent != PurePath("."):
        make_directories(root, relative_path.parent)
    try:
        fd = os.open(root / relative_path, CREATE_FLAGS, FILE_MODE)
    except FileExistsError:
        raise RefusedRead("file.symlink", f"`{relative_path}` exists already (a file, a directory or a link); it is not written through") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def replace_file(root: Path, relative_path: PurePath, data: bytes) -> None:
    """Write `data` over the regular file at `relative_path` under `root`, or create it, never through a link."""
    check_components(root, relative_path)
    path = root / relative_path
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        status = None
    if status is not None and not stat.S_ISREG(status.st_mode):
        raise RefusedRead("file.symlink", f"`{relative_path}` is not a regular file; it is not written through")
    if relative_path.parent != PurePath("."):
        make_directories(root, relative_path.parent)
    try:
        fd = os.open(path, REPLACE_FLAGS, FILE_MODE)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RefusedRead("file.symlink", f"`{relative_path}` is a symbolic link; it is not written through") from None
        raise
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
