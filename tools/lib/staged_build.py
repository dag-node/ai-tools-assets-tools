# SPDX-License-Identifier: MIT
"""The staged set `build-set` writes under the build root, as the commands that read it after find it.

`build-set` renames a staged set over `<build>/<set>` only where the sibling marker `<build>/.<set>.ai-tools-assets-build`
names that set under this marker version, and writes the marker after the rename, so a directory at that name without
the marker is not this command's and is left alone. `sign-set` signs a staged set only where the same marker stands
beside it. An output root may not be `sets/`, hold it or lie inside it, and no existing component of it is a link.
"""
from __future__ import annotations

import os
import pathlib
from typing import Optional

from findings import FindingCollector

INVENTORY_FILE = "SHA256SUMS"
SIGNATURE_FILE = "SHA256SUMS.asc"
INVENTORY_EXCLUDED = (INVENTORY_FILE, SIGNATURE_FILE)
BUILD_MARKER_VERSION = "ai-tools-assets-build 1"


def marker_text(set_name: str) -> str:
    return f"{BUILD_MARKER_VERSION}\nset={set_name}\n"


def marker_path(build_root: pathlib.Path, set_name: str) -> pathlib.Path:
    return build_root / f".{set_name}.ai-tools-assets-build"


def check_output_root(root: pathlib.Path, given: pathlib.Path, what: str, collector: FindingCollector) -> Optional[pathlib.Path]:
    """The absolute output root, or None after a refusal: it does not overlap sets/ and no existing component is a link."""
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


def is_managed_build_directory(build_root: pathlib.Path, set_name: str) -> bool:
    """True when `build/<set>` is a real directory beside a marker naming this set under this marker version."""
    destination = build_root / set_name
    marker = marker_path(build_root, set_name)
    if destination.is_symlink() or not destination.is_dir() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8") == marker_text(set_name)
    except (OSError, UnicodeDecodeError):
        return False
