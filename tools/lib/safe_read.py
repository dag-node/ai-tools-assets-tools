# SPDX-License-Identifier: MIT
"""The one reader for every file a tool opens inside a tree it judges: by descriptor, never through a symbolic link,
once, under a size cap, and judged from the bytes read.

A set is pull-request content, so every open below the directory a command was handed is `openat` from a directory
descriptor with `O_NOFOLLOW`: a symbolic link at any component fails with ELOOP and is refused as `file.symlink`
before a byte of its target is read, and a path swapped between inspection and reading does not redirect the read,
since the read is on the descriptor the inspection used. `fstat` on the open descriptor refuses a non-regular file
(`file.special`) and a file with a second hard link (`file.hardlink`); `O_NONBLOCK` keeps the open of a FIFO from
waiting on a writer. A file is read up to the cap plus one byte whatever its stat said, the SHA-256 is taken over the
bytes read, and the bytes are decoded after, so a decode failure reports `file.binary` with the digest unchanged by it.
The directory a command was handed (`--root`, the parent of `--set-directory`) is the operator's argument, and
`open_root` is the one open that follows a symbolic link.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import List, Optional

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC
READ_CHUNK = 1 << 16


class RefusedRead(Exception):
    """A read the reader refused, with the rule id the refusal lands under and the message for the finding."""

    def __init__(self, rule_id: str, message: str) -> None:
        super().__init__(f"{rule_id}: {message}")
        self.rule_id = rule_id
        self.message = message


@dataclass
class ReadFile:
    """The bytes of one regular file read under a cap; `truncated` when the file held more than the cap."""

    data: bytes
    size: int
    truncated: bool

    @property
    def digest(self) -> Optional[str]:
        """The hex SHA-256 of the whole file, or None when the read stopped at the cap."""
        return None if self.truncated else hashlib.sha256(self.data).hexdigest()


def open_root(path: Path) -> int:
    """Open the directory a command was handed, by path: FileNotFoundError or NotADirectoryError when it is not one."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def open_directory(name: str, dir_fd: int) -> int:
    """Open the child directory `name` of `dir_fd` without following a symbolic link.

    A symbolic link raises RefusedRead(`file.symlink`); FileNotFoundError and NotADirectoryError propagate, since
    which rule a missing or a non-directory entry falls under is the caller's.
    """
    try:
        return os.open(name, DIRECTORY_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        # Linux reports a symbolic link under O_DIRECTORY|O_NOFOLLOW as ENOTDIR, so the entry is classified after the
        # open has already failed; either way it is refused, the lstat decides the rule alone.
        if error.errno == errno.ELOOP or (error.errno == errno.ENOTDIR and _is_symlink(name, dir_fd)):
            raise RefusedRead("file.symlink", "is a symbolic link; a zip or a copy does not carry one the same way on every host") from None
        raise


def _is_symlink(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except OSError:
        return False


def read_file(name: str, dir_fd: int, max_bytes: int) -> ReadFile:
    """Read the regular file `name` under `dir_fd`, at most `max_bytes` + 1 bytes, without following a symbolic link.

    Raises RefusedRead under `file.symlink`, `file.special` or `file.hardlink`; FileNotFoundError propagates.
    """
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RefusedRead("file.symlink", "is a symbolic link; a zip or a copy does not carry one the same way on every host") from None
        if error.errno in (errno.ENXIO, errno.ENODEV):
            raise RefusedRead("file.special", "is not a regular file") from None
        raise
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise RefusedRead("file.special", "is not a regular file or a directory")
        if status.st_nlink > 1:
            raise RefusedRead("file.hardlink", f"has {status.st_nlink} links; a file inside a set has one")
        chunks: List[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(READ_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        return ReadFile(data=data[:max_bytes], size=status.st_size, truncated=True)
    return ReadFile(data=data, size=status.st_size, truncated=False)


def read_file_under(root_fd: int, relative_path: PurePath, max_bytes: int) -> ReadFile:
    """Open each directory of `relative_path` from `root_fd` without following a link, then read the file.

    `relative_path` is one a tool composed from validated names: an absolute path or a `.`/`..` part is a programming
    error and raises ValueError.
    """
    parts = relative_path.parts
    if not parts or relative_path.is_absolute() or any(part in (".", "..") for part in parts):
        raise ValueError(f"not a relative path of plain names: {relative_path}")
    opened: List[int] = []
    try:
        dir_fd = root_fd
        for part in parts[:-1]:
            dir_fd = open_directory(part, dir_fd)
            opened.append(dir_fd)
        return read_file(parts[-1], dir_fd, max_bytes)
    finally:
        for fd in opened:
            os.close(fd)


def decode_text(data: bytes) -> str:
    """The bytes as UTF-8 text; raises RefusedRead(`file.binary`) when they are not."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise RefusedRead("file.binary", "is not UTF-8 text; a set carries text files alone") from None


def read_text_under(root_fd: int, relative_path: PurePath, max_bytes: int) -> str:
    """The text of the file at `relative_path` under `root_fd`; a file over `max_bytes` raises RefusedRead(`file.size`)."""
    read = read_file_under(root_fd, relative_path, max_bytes)
    if read.truncated:
        raise RefusedRead("file.size", f"is {read.size} bytes; a file is at most {max_bytes}")
    return decode_text(read.data)
