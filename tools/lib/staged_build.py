# SPDX-License-Identifier: MIT
"""The staged set `build-set` writes under the build root, as the commands that read it after find it.

`build-set` renames a staged set over `<build>/<set>` only where the sibling marker
`<build>/.<set>.ai-tools-assets-build` names that set under this marker version, and writes the marker after the rename,
so a directory at that name without the marker is not this command's and is left alone. `sign-set` signs a staged set
only where the same marker stands beside it. An output root may not be `sets/`, hold it or lie inside it, and no
existing component of it is a link. Past those checks a command opens the output root once (`open_output_root`) and
works under that descriptor alone: the marker is read through `safe_read`, and the stage is written, replaced, removed
and renamed at the descriptor, so a build root swapped for a link after the checks does not redirect a read, a removal
or a write.
"""
from __future__ import annotations

import os
import pathlib
import stat
from typing import Optional

from findings import FindingCollector
from safe_read import DIRECTORY_FLAGS, RefusedRead, read_file

INVENTORY_FILE = "SHA256SUMS"
SIGNATURE_FILE = "SHA256SUMS.asc"
INVENTORY_EXCLUDED = (INVENTORY_FILE, SIGNATURE_FILE)
BUILD_MARKER_VERSION = "ai-tools-assets-build 1"


def marker_text(set_name: str) -> str:
    return f"{BUILD_MARKER_VERSION}\nset={set_name}\n"


def marker_name(set_name: str) -> str:
    return f".{set_name}.ai-tools-assets-build"


def marker_path(build_root: pathlib.Path, set_name: str) -> pathlib.Path:
    return build_root / marker_name(set_name)


def check_output_root(root: pathlib.Path, given: pathlib.Path, what: str, collector: FindingCollector) -> Optional[pathlib.Path]:
    """The absolute output root, or None after a refusal: it does not overlap sets/ and no existing component is
    a link.

    The `islink` label decides where an output tree is created; it decides nothing a command reads, which goes through
    the descriptor `open_output_root` returns.
    """
    output_root = pathlib.Path(os.path.abspath(root / given))
    sets_root = root / "sets"
    if output_root == sets_root or sets_root in output_root.parents or output_root in sets_root.parents:
        collector.refuse(str(given), "repo.layout", f"the {what} root {output_root} is sets/, holds it or lies inside it; the source tree is not a destination")
        return None
    for component in [output_root, *output_root.parents]:
        try:
            if os.path.islink(component):
                collector.refuse(str(given), "repo.layout", f"`{component}` on the {what} root's path is a symbolic link")
                return None
        except OSError:
            continue
    return output_root


def open_output_root(output_root: pathlib.Path) -> int:
    """The descriptor of an output root `check_output_root` returned, its last component opened without following
    a link; OSError when it is not a directory. The caller closes it."""
    return os.open(output_root, DIRECTORY_FLAGS)


def is_managed_build_directory(build_fd: int, set_name: str) -> bool:
    """True when `<set>` under the build root `build_fd` is a directory and not a link, beside a marker naming this
    set under this marker version.

    The marker is read through `safe_read.read_file` at `build_fd`, bounded by the marker's own length, so a marker that
    is a link, a special file or a file with a second hard link reads as no marker.
    """
    expected = marker_text(set_name).encode("utf-8")
    try:
        if not stat.S_ISDIR(os.lstat(set_name, dir_fd=build_fd).st_mode):
            return False
        read = read_file(marker_name(set_name), build_fd, len(expected))
    except (OSError, RefusedRead):
        return False
    return not read.truncated and read.data == expected
