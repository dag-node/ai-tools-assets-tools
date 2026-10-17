#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# packaging/license-text-digests.py -- prints the table of tools/lib/license_texts.py: each licence's class as the
# table holds it, SPDX's OSI and FSF flags, and the SHA-256 of its text as spdx/license-list-data publishes it.
#
# ```bash
# python3 packaging/license-text-digests.py [--floor v3.20]
# ```
#
# The identifiers and their classes are the table's: a licence joins the table as a line with its class and an empty
# digest tuple, and a run prints every line again. Each `text/<ID>.txt` is fetched at every release tag from `--floor`
# to the newest and at the `master` branch `reuse download` fetches from, and every distinct digest is printed,
# newest first; a tag that does not carry the identifier is skipped, and an identifier no fetch returned is an error.
# The flags are read from `json/licenses.json` at `master`. The output replaces the table's lines. Standard library
# only; reads the network, so the tests do not run it.
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "tools" / "lib"))

from license_texts import LICENSE_TEXTS  # noqa: E402

TAGS_URL = "https://api.github.com/repos/spdx/license-list-data/tags?per_page=100"
DATA_URL = "https://raw.githubusercontent.com/spdx/license-list-data/{ref}/{path}"
BRANCH = "master"
TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?$")
FETCH_WORKERS = 16


def version_of(tag: str):
    match = TAG_PATTERN.match(tag)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def fetch(url: str):
    """The body at `url`, or None for a 404."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the licence table of tools/lib/license_texts.py.")
    parser.add_argument("--floor", default="v3.20", help="the oldest release tag read (default: v3.20)")
    args = parser.parse_args()
    floor = version_of(args.floor)
    if floor is None:
        parser.error(f"--floor takes a release tag such as v3.20, not `{args.floor}`")
    tags = [tag["name"] for tag in json.loads(fetch(TAGS_URL))]
    releases = sorted((tag for tag in tags if version_of(tag) and version_of(tag) >= floor), key=version_of, reverse=True)
    refs = (BRANCH, *releases)
    flags = {entry["licenseId"]: entry for entry in json.loads(fetch(DATA_URL.format(ref=BRANCH, path="json/licenses.json")))["licenses"]}
    requests = [(identifier, ref) for identifier in sorted(LICENSE_TEXTS, key=str.lower) for ref in refs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        bodies = dict(zip(requests, pool.map(lambda request: fetch(DATA_URL.format(ref=request[1], path=f"text/{request[0]}.txt")), requests)))
    status = 0
    for identifier in sorted(LICENSE_TEXTS, key=str.lower):
        digests = []
        for ref in refs:
            body = bodies[(identifier, ref)]
            if body is not None and hashlib.sha256(body).hexdigest() not in digests:
                digests.append(hashlib.sha256(body).hexdigest())
        entry = flags.get(identifier)
        if not digests or entry is None:
            print(f"license-text-digests: {identifier}: not on the SPDX list at {BRANCH}, or no text at any tag from {args.floor}", file=sys.stderr)
            status = 1
            continue
        osi, fsf = bool(entry.get("isOsiApproved")), bool(entry.get("isFsfLibre"))
        digest_tuple = "(" + "".join(f'"{digest}", ' for digest in digests).rstrip(" ") + ")"
        print(f'    "{identifier}": LicenseText({LICENSE_TEXTS[identifier].license_class.upper().replace("-", "_")}, osi={osi}, fsf={fsf}, digests={digest_tuple}),')
    print(f"# from {BRANCH} and the tags {', '.join(releases)}", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
