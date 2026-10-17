# SPDX-License-Identifier: MIT
"""Removes a scratch tree that holds a GnuPG home a test generated a key in.

`gpg` starts `gpg-agent`, and the agent starts `scdaemon`; each daemon unlinks its socket in the home as it exits, which
can be after `gpgconf --kill` has returned, so a removal walking the tree meets an entry that vanished between listing
and unlink. Python 3.9's `shutil.rmtree` raises `FileNotFoundError` there.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess


def stop_daemons(home: pathlib.Path) -> None:
    """Stops every daemon `gpgconf` knows for `home`; a `gpgconf` that fails or is absent leaves them to the removal."""
    try:
        subprocess.run(["gpgconf", "--kill", "all"], env=dict(os.environ, GNUPGHOME=str(home)), capture_output=True)
    except OSError:
        pass


def remove_tree(path: pathlib.Path, homes=()) -> None:
    """Stops the daemons of each home in `homes`, then removes `path`, walking it again where an entry vanished."""
    for home in homes:
        stop_daemons(home)
    for _ in range(3):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            if not path.exists():
                return
    shutil.rmtree(path)
