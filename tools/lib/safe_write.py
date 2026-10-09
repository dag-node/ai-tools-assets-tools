# SPDX-License-Identifier: MIT
"""The writer every scaffolding and build command creates files through: by directory descriptor, never through a
symbolic link, a new file created exclusively, and an existing inode never written into.

A command that writes under a publisher repository walks a tree a pull request may have shaped, so the directory a
file goes into is opened component by component from the root with `O_DIRECTORY|O_NOFOLLOW`, as `safe_read` opens
one for reading: a symbolic link at a component refuses (`file.symlink`), an entry that is not a directory refuses
(`file.special`), a missing directory is created with `mkdir` at the descriptor, and the file is then created at that
descriptor, so a component swapped for a link after it was opened does not redirect the write -- the write lands in
the directory the inspection opened. A new file is opened with `O_CREAT|O_EXCL|O_NOFOLLOW`, so a dangling link or any
other entry at the name fails with EEXIST instead of being written through; and a generated file that is rewritten is
replaced as a directory entry, never written into: the entry is `lstat`ed at the descriptor, a link, a directory or a
special file at the name (`file.symlink`) or a regular file with a second hard link (`file.hardlink`, since the other
name may lie outside the tree) refuses, the bytes go to `.<name>.<random>` beside it, created exclusively, and that
name is renamed over the entry at the same descriptor. The root a command was handed is the operator's argument and is
the one open that follows a link, as `safe_read.open_root` is. Every refusal is a `safe_read.RefusedRead`, so a
command reports it under the rule the caller names.
"""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path, PurePath

from safe_read import RefusedRead, open_directory, open_root

CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_MODE = 0o644
DIRECTORY_MODE = 0o755


def open_parent(root: Path, relative_path: PurePath) -> int:
    """A descriptor on the directory that holds the last component of `relative_path`, opened component by component
    from `root` without following a symbolic link, each missing directory created as it is reached.

    A link at a component refuses (`file.symlink`); an entry that is not a directory refuses (`file.special`). The
    caller closes the descriptor. `relative_path` is one a tool composed from validated names: an absolute path or a
    `.`/`..` part is a programming error and raises ValueError.
    """
    parts = relative_path.parts
    if not parts or relative_path.is_absolute() or any(part in (".", "..") for part in parts):
        raise ValueError(f"not a relative path of plain names: {relative_path}")
    dir_fd = open_root(root)
    for depth, part in enumerate(parts[:-1], start=1):
        dir_fd = _descend(part, dir_fd, PurePath(*parts[:depth]))
    return dir_fd


def _descend(name: str, dir_fd: int, shown: PurePath) -> int:
    """The child directory `name` of `dir_fd`, opened without following a link and created where it is missing;
    `dir_fd` is closed whatever the outcome, and `shown` names the component in a refusal."""
    try:
        try:
            return _open_child(name, dir_fd, shown)
        except FileNotFoundError:
            pass
        try:
            os.mkdir(name, DIRECTORY_MODE, dir_fd=dir_fd)
        except FileExistsError:
            pass  # placed meanwhile; the open that follows judges what stands there
        return _open_child(name, dir_fd, shown)
    finally:
        os.close(dir_fd)


def _open_child(name: str, dir_fd: int, shown: PurePath) -> int:
    try:
        return open_directory(name, dir_fd)
    except RefusedRead:
        raise RefusedRead("file.symlink", f"`{shown}` is a symbolic link; a write does not go through one") from None
    except NotADirectoryError:
        raise RefusedRead("file.special", f"`{shown}` is not a directory") from None


def create_file(root: Path, relative_path: PurePath, data: bytes) -> None:
    """Create the file exclusively under `root`, with its missing directories; an entry already at the name refuses."""
    parent_fd = open_parent(root, relative_path)
    try:
        try:
            fd = os.open(relative_path.name, CREATE_FLAGS, FILE_MODE, dir_fd=parent_fd)
        except FileExistsError:
            raise RefusedRead("file.symlink", f"`{relative_path}` exists already (a file, a directory or a link); it is not written through") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    finally:
        os.close(parent_fd)


def replace_file(root: Path, relative_path: PurePath, data: bytes) -> None:
    """Replace the entry at `relative_path` under `root` with a new regular file holding `data`, or create it.

    The entry is `lstat`ed at its directory's descriptor and refused when it is not a regular file or has a second
    hard link; the bytes are written to a fresh exclusively created name in the same directory and renamed over the
    entry there, so the inode that stood there is never written into. A failed write removes the temporary name.
    """
    name = relative_path.name
    parent_fd = open_parent(root, relative_path)
    try:
        try:
            status = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            status = None
        if status is not None:
            if not stat.S_ISREG(status.st_mode):
                raise RefusedRead("file.symlink", f"`{relative_path}` is not a regular file; it is not written through")
            if status.st_nlink > 1:
                raise RefusedRead("file.hardlink", f"`{relative_path}` has {status.st_nlink} links; a generated file has one, and the inode is not written into")
        temporary = f".{name}.{secrets.token_hex(8)}"
        try:
            fd = os.open(temporary, CREATE_FLAGS, FILE_MODE, dir_fd=parent_fd)
        except FileExistsError:
            raise RefusedRead("file.symlink", f"`{relative_path.parent / temporary}` exists already; the replacement is not written") from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(parent_fd)
