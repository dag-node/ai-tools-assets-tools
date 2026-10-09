#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# packaging/render-nfpm.py -- writes the nFPM configuration of a set's RPM from nfpm-set.yaml.in, the staged set's
# set.conf and the repository's publisher.conf, or of this repository's tools RPM from nfpm-tools.yaml.in.
#
# ```bash
# python3 packaging/render-nfpm.py set --set <set> --version <version> --staged build/<set> --dist el9 --output <file>
# python3 packaging/render-nfpm.py tools --version <version> --dist el9 --output <file>
# ```
#
# `--dist` is the dist tag of one served distribution (el9, fc44), and the package's Release is `1.<dist>`:
# dag-node/rpm places a package into the tree its file name's dist tag names and skips a package without one, so
# release-steps.sh build-rpm renders one configuration per distribution it lists.
#
# The release workflow runs it after tools/build-set has validated and staged the set, so the values come from the
# set.conf that ships. It refuses a set.conf the KEY=value reader reports a problem in, a `name` other than `--set` and
# a `version` other than `--version`, which the workflow takes from the tag, so the package version is the tag's. Each
# `@KEY@` in the template is replaced in one pass by its value as a JSON string, which YAML reads as a double-quoted
# scalar: a quote, a colon or a `@KEY@` inside a value stays text and does not reach nFPM as configuration. A template
# naming a key this script does not supply is refused. The tools RPM takes the version alone, which the workflow
# takes from the tag `v<version>`; it is held to semantic versioning here. Standard library only.
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Dict

PACKAGING = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGING.parent / "tools" / "lib"))

from asset_format import SEMVER_PATTERN  # noqa: E402
from key_value_config import KeyValueDocument, parse_key_value_text  # noqa: E402

PLACEHOLDER = re.compile(r"@([A-Z_]+)@")
DIST_TAG = re.compile(r"(el|fc)[1-9][0-9]*")
PACKAGE_PREFIX = "ai-tools-assets-"
INSTALL_ROOT = "/usr/share/ai-tools-assets"


class RenderError(Exception):
    pass


def read_config(path: pathlib.Path) -> KeyValueDocument:
    try:
        document = parse_key_value_text(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RenderError(f"{path}: {error.strerror}") from None
    problems = [f"line {number}: {reason}" for number, reason in document.malformed_lines]
    problems += [f"line {number}: `{key}` is given twice" for key, number in document.duplicate_keys]
    if problems:
        raise RenderError(f"{path}: " + "; ".join(problems))
    return document


def render(template: str, values: Dict[str, str]) -> str:
    """The template with every `@KEY@` replaced by its value as a JSON string, in one pass."""
    missing = sorted(set(PLACEHOLDER.findall(template)) - set(values))
    if missing:
        raise RenderError(f"the template names {', '.join(missing)}, which this script does not supply")
    return PLACEHOLDER.sub(lambda match: json.dumps(values[match.group(1)]), template)


def release(dist: str) -> str:
    if not DIST_TAG.fullmatch(dist):
        raise RenderError(f"`{dist}` is not a dist tag elN or fcN")
    return f"1.{dist}"


def set_values(root: pathlib.Path, set_name: str, version: str, staged: pathlib.Path, dist: str) -> Dict[str, str]:
    set_conf = read_config(staged / "set.conf")
    publisher_conf = read_config(root / "publisher.conf")
    if set_conf.get("name") != set_name:
        raise RenderError(f"{staged / 'set.conf'}: name is `{set_conf.get('name')}`, not the set `{set_name}`")
    if set_conf.get("version") != version:
        raise RenderError(f"{staged / 'set.conf'}: version is `{set_conf.get('version')}`, not the tag's `{version}`")
    publisher = publisher_conf.get("publisher")
    return {
        "NAME": PACKAGE_PREFIX + set_name,
        "VERSION": version,
        "RELEASE": release(dist),
        "SUMMARY": set_conf.get("summary"),
        "LICENSE": set_conf.get("license"),
        "MAINTAINER": f"{publisher} <{publisher_conf.get('contact')}>",
        "VENDOR": publisher,
        "HOMEPAGE": set_conf.get("source"),
        "STAGED": str(staged.resolve()),
        "INSTALL_ROOT": INSTALL_ROOT,
        "DESTINATION": f"{INSTALL_ROOT}/{set_name}",
    }


def tools_values(version: str, dist: str) -> Dict[str, str]:
    if not SEMVER_PATTERN.match(version):
        raise RenderError(f"`{version}` is not a semantic version")
    return {"VERSION": version, "RELEASE": release(dist)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the nFPM configuration of a set's RPM.")
    commands = parser.add_subparsers(dest="command", required=True)
    set_command = commands.add_parser("set", help="a set's RPM, from the staged set")
    set_command.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."), help="the publisher repository")
    set_command.add_argument("--set", dest="set_name", required=True, help="the set the tag names")
    set_command.add_argument("--version", required=True, help="the version the tag names")
    set_command.add_argument("--staged", type=pathlib.Path, required=True, help="the set tools/build-set staged")
    tools_command = commands.add_parser("tools", help="this repository's tools RPM")
    tools_command.add_argument("--version", required=True, help="the version the tag names")
    for command in (set_command, tools_command):
        command.add_argument("--dist", required=True, help="the dist tag of the RPM's distribution, elN or fcN")
        command.add_argument("--output", type=pathlib.Path, required=True, help="the configuration file to write")
    arguments = parser.parse_args()
    try:
        if arguments.command == "set":
            values = set_values(arguments.root, arguments.set_name, arguments.version, arguments.staged, arguments.dist)
        else:
            values = tools_values(arguments.version, arguments.dist)
        template = PACKAGING / f"nfpm-{arguments.command}.yaml.in"
        text = render(template.read_text(encoding="utf-8"), values)
        arguments.output.write_text(text, encoding="utf-8")
    except RenderError as error:
        print(f"render-nfpm: {error}", file=sys.stderr)
        return 1
    print(f"render-nfpm: wrote {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
