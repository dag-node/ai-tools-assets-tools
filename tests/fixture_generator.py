# SPDX-License-Identifier: MIT
"""Writes the conformance fixtures under fixtures/, or checks that the committed ones are what it writes.

    python3 tests/fixture_generator.py generate     write fixtures/pass/* and fixtures/fail/*
    python3 tests/fixture_generator.py check        exit 1 when a committed fixture differs from what generate writes

Every fixture is one publisher's set, written from one base tree so a failing fixture differs from the passing one in
the one thing its rule refuses. A fixture directory holds `fixture.conf` -- `expect` (pass, fail or warn), `rule`
(the rule id a failing or warning fixture reports), `publisher`, `profile`, `licenses` and `license_texts`, the options
`tools/validate --set-directory` is run with -- and beside it the set directory. A rule's first fixture is
`fail/<rule>/`; a further shape the same rule refuses is `fail/<rule>.<variant>/`, with the rule in its
`fixture.conf`. The licence texts the fixtures share
sit in fixtures/LICENSES/ and are not generated. The fixtures are committed so a consumer -- ai-tools-base's CI runs
its own validator over them at the pinned release -- reads them without running this script; `check` keeps the
committed copies current. Three rules take a tree git does not carry (a hard link, a special file, an over-size file),
and tests/test_fixtures.py builds those in a temporary directory instead.
"""
from __future__ import annotations

import filecmp
import json
import os
import pathlib
import shutil
import sys
import tempfile
from typing import Callable, Dict, List, Tuple, Union

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
sys.path.insert(0, str(REPOSITORY / "tools" / "lib"))

from key_value_config import parse_key_value_text  # noqa: E402
from manifests import claude_plugin_document, portable_plugin_document, render_json  # noqa: E402

FIXTURES = REPOSITORY / "fixtures"
FileContent = Union[str, bytes]
Tree = Dict[str, FileContent]
Mutation = Callable[[Tree], None]

PUBLISHER_CONF = """publisher={publisher}
contact=tools@{publisher}.example
maintainers=[maintainers@{publisher}.example]
source=https://github.com/{publisher}/ai-tools-assets
description=Skills and subagents for coding agents, released as versioned sets
"""

SET_CONF = """# ai-tools-assets set: {set_name}. Read by ai-tools-base as KEY=value
# data; not executed. Keys: ai-tools-assets(5).
format=1
name={set_name}
version=0.1.0
summary="Fixture skills and subagents"
license=MIT
maintainers=[maintainers@{publisher}.example]
source=https://github.com/{publisher}/ai-tools-assets
"""

SKILL_MD = """---
name: {prefix}pdf-processing
description: Extract text and tables from PDF files and fill PDF forms. Use when a task reads or edits a PDF.
compatibility: Requires python3.
metadata:
  ai-tools-libs: python/pdftext
---

# PDF processing

Run `python3 scripts/extract.py <file>` to extract text, and
`dotnet run scripts/report.cs -- <file>` for a table report. Formats are
listed in `references/formats.md`.
"""

VENDORED_SKILL_MD = """---
name: pdftext
description: Reads the text layer of a PDF. Use when a PDF holds selectable text.
license: CC0-1.0
---

# pdftext

Run `python3 scripts/pdftext.py <file>`.
"""

UPSTREAM_CONF = """source=https://github.com/example-org/skills
path=skills/pdftext
revision=4f2c9d0e1b7a3c5d6e8f90a1b2c3d4e5f6a7b8c94f2c9d0e1b7a3c5d6e8f90a1
license=CC0-1.0
"""

SUBAGENT_MD = """---
name: {prefix}reviewer
description: Reviews a change for the conventions this set states. Use after a change is staged.
tools: [Read, Grep]
model: inherit
---

You review a staged change against the conventions of the repository and
report what departs from them.
"""

VENDORED_SUBAGENT_MD = """---
name: upstream-triage
description: Sorts findings by severity. Use when a report holds more than ten findings.
---

You sort findings by severity and return the ten most severe.
"""

VENDORED_SUBAGENT_UPSTREAM = """source=https://github.com/example-org/agents
path=agents/triage.md
revision=0123456789abcdef0123456789abcdef01234567
license=CC0-1.0
"""

ASSET_CONF = """format=1
requires_capabilities=[subagents.claude.v1]
targets=[claude-code]
"""

REPORT_CS = """#!/usr/bin/env dotnet
#:sdk Microsoft.NET.Sdk
#:property Nullable=enable
using System;
Console.WriteLine(args.Length);
"""

# The fixture scripts carry a header of their own; `reuse lint` reads the literal here as this file's second
# expression, so the two constants sit in an ignored block.
# REUSE-IgnoreStart
EXTRACT_PY = """# SPDX-License-Identifier: MIT
import sys
print(sys.argv[1:])
"""

TEST_EXTRACT_PY = """# SPDX-License-Identifier: MIT
def test_nothing():
    assert True
"""
# REUSE-IgnoreEnd


def base_tree(publisher: str, set_name: str) -> Tree:
    """The passing set for `publisher`, named `set_name`, with its authored and vendored assets."""
    prefix = "ai-tools-" if set_name in ("core", "ai-tools") else f"{set_name}-"
    publisher_conf = parse_key_value_text(PUBLISHER_CONF.format(publisher=publisher))
    set_conf = parse_key_value_text(SET_CONF.format(set_name=set_name, publisher=publisher))
    tree: Tree = {
        "set.conf": SET_CONF.format(set_name=set_name, publisher=publisher),
        "CHANGELOG.md": f"# Changelog: {set_name}\n\n## [Unreleased]\n\n### Added\n\n- The fixture set.\n",
        "README.md": f"# {set_name}\n\nA fixture set.\n",
        "plugin.json": render_json(portable_plugin_document(set_conf, publisher_conf)),
        ".claude-plugin/plugin.json": render_json(claude_plugin_document(set_conf, publisher_conf)),
        f"skills/{prefix}pdf-processing/SKILL.md": SKILL_MD.format(prefix=prefix),
        f"skills/{prefix}pdf-processing/scripts/extract.py": EXTRACT_PY,
        f"skills/{prefix}pdf-processing/scripts/report.cs": REPORT_CS,
        f"skills/{prefix}pdf-processing/references/formats.md": "# Formats\n\nPDF 1.4 to 2.0.\n",
        f"skills/{prefix}pdf-processing/tests/test_extract.py": TEST_EXTRACT_PY,
        "skills/pdftext/SKILL.md": VENDORED_SKILL_MD,
        "skills/pdftext/UPSTREAM.conf": UPSTREAM_CONF,
        "skills/pdftext/scripts/pdftext.py": "print('text')\n",
        f"agents/{prefix}reviewer.md": SUBAGENT_MD.format(prefix=prefix),
        "agents/upstream-triage.md": VENDORED_SUBAGENT_MD,
        "metadata/subagents/upstream-triage/UPSTREAM.conf": VENDORED_SUBAGENT_UPSTREAM,
        "metadata/subagents/upstream-triage/asset.conf": ASSET_CONF,
    }
    return tree


def rerender_manifests(tree: Tree, publisher: str, set_name: str) -> None:
    """Write the two plugin manifests again from the tree's set.conf, so a mutated set.conf fails on its own rule.

    The manifest name follows the set directory, as the validator expects it, whatever `name=` the mutation wrote.
    """
    if "set.conf" not in tree or not isinstance(tree["set.conf"], str):
        return
    set_conf = parse_key_value_text(tree["set.conf"])
    set_conf.values["name"] = set_name
    publisher_conf = parse_key_value_text(PUBLISHER_CONF.format(publisher=publisher))
    tree["plugin.json"] = render_json(portable_plugin_document(set_conf, publisher_conf))
    tree[".claude-plugin/plugin.json"] = render_json(claude_plugin_document(set_conf, publisher_conf))


def replace_in(tree: Tree, path: str, old: str, new: str) -> None:
    text = tree[path]
    assert isinstance(text, str) and old in text, f"{path} does not hold {old!r}"
    tree[path] = text.replace(old, new)


def rename(tree: Tree, old_prefix: str, new_prefix: str) -> None:
    for path in [path for path in tree if path.startswith(old_prefix)]:
        tree[new_prefix + path[len(old_prefix):]] = tree.pop(path)


def with_component(tree: Tree, key: str) -> None:
    document = json.loads(tree["plugin.json"])
    document[key] = {}
    tree["plugin.json"] = json.dumps(document, indent=2) + "\n"


SKILL = "skills/acme-pdf-processing"

# (rule id, expectation, mutation). Each mutation changes the base tree in the one way the rule refuses.
FAIL_FIXTURES: List[Tuple[str, str, Mutation]] = [
    ("set.conf.missing", "fail", lambda tree: tree.pop("set.conf")),
    ("set.conf.syntax", "fail", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", "license=MIT\nbad line\nname=acme\n")),
    ("set.conf.required-key", "fail", lambda tree: replace_in(tree, "set.conf", 'summary="Fixture skills and subagents"\n', "")),
    ("set.conf.format", "fail", lambda tree: replace_in(tree, "set.conf", "format=1", "format=2")),
    ("set.conf.name", "fail", lambda tree: replace_in(tree, "set.conf", "name=acme\n", "name=acme-other\n")),
    ("set.conf.version", "fail", lambda tree: replace_in(tree, "set.conf", "version=0.1.0", "version=1.0")),
    ("set.conf.requires-capabilities", "fail", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", "license=MIT\nrequires_capabilities=[skills.future.v9]\n")),
    ("set.conf.integrations", "fail", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", "license=MIT\nintegrations=[dotnet]\n")),
    ("set.conf.unknown-key", "warn", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", "license=MIT\nhomepage=https://acme.example\n")),
    ("set.entry.unknown", "fail", lambda tree: tree.__setitem__("notes.txt", "stray\n")),
    ("set.entry.reserved", "fail", lambda tree: tree.__setitem__("libs/python/pdftext.py", "print()\n")),
    ("set.manifest.plugin", "fail", lambda tree: tree.__setitem__("plugin.json", tree["plugin.json"].replace('"0.1.0"', '"0.2.0"'))),
    ("set.manifest.components", "fail", lambda tree: with_component(tree, "hooks")),
    ("set.manifest.claude-plugin", "fail", lambda tree: tree.__setitem__(".claude-plugin/hooks.json", "{}\n")),
    ("name.grammar", "fail", lambda tree: (rename(tree, SKILL, "skills/Acme-PDF"), replace_in(tree, "skills/Acme-PDF/SKILL.md", "name: acme-pdf-processing", "name: Acme-PDF"))),
    ("name.reserved-claude", "fail", lambda tree: (rename(tree, SKILL, "skills/acme-claude-helper"), replace_in(tree, "skills/acme-claude-helper/SKILL.md", "name: acme-pdf-processing", "name: acme-claude-helper"))),
    ("name.asset-prefix", "fail", lambda tree: (rename(tree, SKILL, "skills/pdf-processing"), replace_in(tree, "skills/pdf-processing/SKILL.md", "name: acme-pdf-processing", "name: pdf-processing"))),
    ("name.frontmatter", "fail", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "name: acme-pdf-processing", "name: acme-other")),
    ("name.collision", "fail", lambda tree: tree.__setitem__("agents/acme-pdf-processing.md", SUBAGENT_MD.format(prefix="acme-").replace("acme-reviewer", "acme-pdf-processing"))),
    ("name.composed-length", "warn", lambda tree: (rename(tree, SKILL, "skills/acme-" + "x" * 55), replace_in(tree, "skills/acme-" + "x" * 55 + "/SKILL.md", "name: acme-pdf-processing", "name: acme-" + "x" * 55))),
    ("kind.shape", "fail", lambda tree: tree.__setitem__("skills/notes.md", "# stray\n")),
    ("kind.reserved", "fail", lambda tree: tree.__setitem__("commands/acme-run.md", "# command\n")),
    ("skill.entry.unknown", "fail", lambda tree: tree.__setitem__(f"{SKILL}/extra.txt", "stray\n")),
    ("skill.plugin-manifest", "fail", lambda tree: tree.__setitem__(f"{SKILL}/.claude-plugin/plugin.json", '{"name": "x"}\n')),
    ("skill.sidecar", "fail", lambda tree: tree.__setitem__(f"{SKILL}/agents/openai.yaml", "interface:\n  display_name: PDF\n")),
    ("skill.length", "warn", lambda tree: tree.__setitem__(f"{SKILL}/SKILL.md", tree[f"{SKILL}/SKILL.md"] + "\nMore text.\n" * 260)),
    ("frontmatter.missing", "fail", lambda tree: tree.__setitem__(f"{SKILL}/SKILL.md", "# PDF processing\n\nNo frontmatter.\n")),
    ("frontmatter.syntax", "fail", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "compatibility: Requires python3.", "compatibility: |\n  block scalar")),
    ("frontmatter.required", "fail", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "description: Extract text and tables from PDF files and fill PDF forms. Use when a task reads or edits a PDF.", "description:")),
    ("frontmatter.refused-key", "fail", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "compatibility: Requires python3.", "allowed-tools: Bash")),
    ("frontmatter.length", "fail", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "Use when a task reads or edits a PDF.", "Use when a task reads or edits a PDF. " + "x" * 1024)),
    ("frontmatter.metadata", "fail", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "metadata:\n  ai-tools-libs: python/pdftext", "metadata: python/pdftext")),
    ("frontmatter.type", "fail", lambda tree: replace_in(tree, "skills/pdftext/SKILL.md", "license: CC0-1.0", "license: [GPL-3.0-only]")),
    ("frontmatter.metadata-prefix", "warn", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "ai-tools-libs: python/pdftext", "vendor-note: python/pdftext")),
    ("body.dynamic-injection", "fail", lambda tree: tree.__setitem__(f"{SKILL}/SKILL.md", tree[f"{SKILL}/SKILL.md"] + "\n!`date`\n")),
    ("body.absolute-path", "fail", lambda tree: tree.__setitem__(f"{SKILL}/references/formats.md", "# Formats\n\nSee /opt/ai-tools/skills/other/README.md.\n")),
    ("file.binary", "fail", lambda tree: tree.__setitem__(f"{SKILL}/assets/logo.bin", b"\x00\x01\x02binary")),
    ("file.reserved-name", "fail", lambda tree: tree.__setitem__(f"{SKILL}/scripts/SHA256SUMS", "not an inventory\n")),
    ("cs.package", "fail", lambda tree: replace_in(tree, f"{SKILL}/scripts/report.cs", "#:property Nullable=enable", "#:package Newtonsoft.Json@13.0.3")),
    ("cs.sdk", "fail", lambda tree: replace_in(tree, f"{SKILL}/scripts/report.cs", "#:sdk Microsoft.NET.Sdk", "#:sdk Aspire.AppHost.Sdk")),
    ("cs.project", "fail", lambda tree: replace_in(tree, f"{SKILL}/scripts/report.cs", "#:property Nullable=enable", "#:project ../../../../Shared/Shared.csproj")),
    ("provenance.syntax", "fail", lambda tree: replace_in(tree, "skills/pdftext/UPSTREAM.conf", "revision=4f2c9d0e1b7a3c5d6e8f90a1b2c3d4e5f6a7b8c94f2c9d0e1b7a3c5d6e8f90a1\n", "")),
    ("provenance.duplicate", "fail", lambda tree: tree.__setitem__("metadata/skills/pdftext/UPSTREAM.conf", UPSTREAM_CONF)),
    ("metadata.kind", "fail", lambda tree: tree.__setitem__("metadata/hooks/acme-hook/asset.conf", ASSET_CONF)),
    ("metadata.asset", "fail", lambda tree: tree.__setitem__("metadata/skills/acme-missing/asset.conf", "format=1\n")),
    ("metadata.entry", "fail", lambda tree: tree.__setitem__("metadata/subagents/upstream-triage/notes.md", "# notes\n")),
    ("metadata.asset-conf", "fail", lambda tree: replace_in(tree, "metadata/subagents/upstream-triage/asset.conf", "subagents.claude.v1", "hooks.v1")),
    ("license.expression", "fail", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", "license=\"MIT OR\"\n")),
    ("license.allowlist", "fail", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", "license=GPL-3.0-only\n")),
    ("license.text", "fail", lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "compatibility: Requires python3.", "license: Apache-2.0")),
]


# (fixture name, rule id, mutation): a further shape a rule refuses, beside the rule's first fixture.
VARIANT_FIXTURES: List[Tuple[str, str, Mutation]] = [
    ("body.dynamic-injection.inline", "body.dynamic-injection",
     lambda tree: tree.__setitem__(f"{SKILL}/SKILL.md", tree[f"{SKILL}/SKILL.md"] + "\n- Current date: !`date`\n")),
    ("body.dynamic-injection.fence", "body.dynamic-injection",
     lambda tree: tree.__setitem__(f"{SKILL}/SKILL.md", tree[f"{SKILL}/SKILL.md"] + "\nMore text.\n\n```!\ngit status\n```\n")),
    ("body.dynamic-injection.fence-tagged", "body.dynamic-injection",
     lambda tree: tree.__setitem__(f"{SKILL}/SKILL.md", tree[f"{SKILL}/SKILL.md"] + "\n```bash!\ngit status\n```\n")),
    ("body.dynamic-injection.subagent", "body.dynamic-injection",
     lambda tree: tree.__setitem__("agents/acme-reviewer.md", tree["agents/acme-reviewer.md"] + "\nThe branch: !`git branch --show-current`\n")),
    ("license.expression.nested", "license.expression",
     lambda tree: replace_in(tree, "set.conf", "license=MIT\n", 'license="' + "(" * 600 + "MIT" + ")" * 600 + '"\n')),
    ("license.expression.long", "license.expression",
     lambda tree: replace_in(tree, "set.conf", "license=MIT\n", 'license="' + " AND ".join(["MIT"] * 40) + '"\n')),
    ("set.manifest.plugin.nested", "set.manifest.plugin", lambda tree: tree.__setitem__("plugin.json", "[" * 10000 + "]" * 10000 + "\n")),
    ("set.manifest.plugin.large", "set.manifest.plugin",
     lambda tree: tree.__setitem__("plugin.json", tree["plugin.json"].rstrip("}\n") + ',\n  "note": "' + "x" * (64 * 1024) + '"\n}\n')),
    ("file.binary.c1-control", "file.binary", lambda tree: tree.__setitem__(f"{SKILL}/references/formats.md", "# Formats\n\nPDF" + chr(0x85) + "1.4.\n")),
    ("file.binary.bidi-mark", "file.binary", lambda tree: tree.__setitem__(f"{SKILL}/references/formats.md", "# Formats\n\n" + chr(0x200E) + "PDF 1.4.\n")),
    ("file.binary.arabic-letter-mark", "file.binary", lambda tree: tree.__setitem__(f"{SKILL}/references/formats.md", "# Formats\n\n" + chr(0x061C) + "PDF 1.4.\n")),
    ("file.binary.bom-first", "file.binary", lambda tree: tree.__setitem__(f"{SKILL}/SKILL.md", chr(0xFEFF) + tree[f"{SKILL}/SKILL.md"])),
    ("file.binary.bom-inside", "file.binary", lambda tree: tree.__setitem__(f"{SKILL}/references/formats.md", "# Formats\n\nPDF " + chr(0xFEFF) + "1.4.\n")),
    ("kind.shape.kind-root-file", "kind.shape", lambda tree: (remove_under(tree, "skills/"), tree.__setitem__("skills", "not a directory\n"))),
    ("kind.shape.entry-file-directory", "kind.shape",
     lambda tree: (tree.pop(f"{SKILL}/SKILL.md"), tree.__setitem__(f"{SKILL}/SKILL.md/notes.md", "# inside a directory named SKILL.md\n"))),
    ("set.entry.unknown.licenses-file", "set.entry.unknown", lambda tree: tree.__setitem__("LICENSES", "not a directory\n")),
    ("skill.entry.unknown.scripts-file", "skill.entry.unknown",
     lambda tree: (remove_under(tree, f"{SKILL}/scripts/"), tree.__setitem__(f"{SKILL}/scripts", "not a directory\n"))),
    ("metadata.entry.references-file", "metadata.entry",
     lambda tree: tree.__setitem__("metadata/subagents/upstream-triage/references", "not a directory\n")),
    ("cs.project.missing", "cs.project",
     lambda tree: replace_in(tree, f"{SKILL}/scripts/report.cs", "#:property Nullable=enable", "#:project ../assets/Shared.csproj")),
    ("cs.project.not-csproj", "cs.project",
     lambda tree: (replace_in(tree, f"{SKILL}/scripts/report.cs", "#:property Nullable=enable", "#:project ../assets/Shared.props"),
                   tree.__setitem__(f"{SKILL}/assets/Shared.props", "<Project />\n"))),
    ("set.manifest.plugin.unknown-key", "set.manifest.plugin", lambda tree: with_component(tree, "futureServer")),
    ("set.conf.syntax.unclosed-quote", "set.conf.syntax", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", 'license="MIT\n')),
    ("set.conf.syntax.text-after-quote", "set.conf.syntax", lambda tree: replace_in(tree, "set.conf", "license=MIT\n", 'license="MIT"garbage\n')),
    ("set.conf.syntax.empty-list-item", "set.conf.syntax",
     lambda tree: replace_in(tree, "set.conf", "maintainers=[maintainers@acme.example]", "maintainers=[maintainers@acme.example,,other@acme.example]")),
    ("set.conf.syntax.empty-list-value", "set.conf.syntax",
     lambda tree: replace_in(tree, "set.conf", "maintainers=[maintainers@acme.example]", "maintainers=")),
    ("set.conf.required-key.empty-list", "set.conf.required-key",
     lambda tree: replace_in(tree, "set.conf", "maintainers=[maintainers@acme.example]", "maintainers=[]")),
    ("frontmatter.syntax.colon", "frontmatter.syntax",
     lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "compatibility: Requires python3.", "compatibility: Requires: python3")),
    ("frontmatter.syntax.escape", "frontmatter.syntax",
     lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "compatibility: Requires python3.", 'compatibility: "Requires \\qpython3."')),
    ("frontmatter.syntax.alias-item", "frontmatter.syntax",
     lambda tree: replace_in(tree, "agents/acme-reviewer.md", "tools: [Read, Grep]", "tools: [*alias, Grep]")),
    ("frontmatter.syntax.empty-item", "frontmatter.syntax",
     lambda tree: replace_in(tree, "agents/acme-reviewer.md", "tools: [Read, Grep]", "tools: [Read,,Grep]")),
    ("frontmatter.type.metadata-boolean", "frontmatter.type",
     lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "ai-tools-libs: python/pdftext", "ai-tools-libs: true")),
    ("frontmatter.type.description-number", "frontmatter.type",
     lambda tree: replace_in(tree, f"{SKILL}/SKILL.md", "compatibility: Requires python3.", "compatibility: 3.12")),
    ("frontmatter.type.max-turns", "frontmatter.type",
     lambda tree: replace_in(tree, "agents/acme-reviewer.md", "model: inherit", 'maxTurns: "3"')),
    ("frontmatter.type.tools-map", "frontmatter.type",
     lambda tree: replace_in(tree, "agents/acme-reviewer.md", "tools: [Read, Grep]", "tools:\n  read: yes")),
]

# (fixture name, mutation): a shape the format accepts beside the plain passing set, under fixtures/pass/.
PASS_VARIANT_FIXTURES: List[Tuple[str, Mutation]] = [
    ("acme.cs-project-inside", lambda tree: (
        replace_in(tree, f"{SKILL}/scripts/report.cs", "#:property Nullable=enable", "#:project ../assets/Shared.csproj"),
        tree.__setitem__(f"{SKILL}/assets/Shared.csproj", '<Project Sdk="Microsoft.NET.Sdk" />\n'))),
    ("acme.explicit-empty-list", lambda tree: (
        replace_in(tree, "metadata/subagents/upstream-triage/asset.conf", "targets=[claude-code]\n", "targets=[claude-code]\nrequires_integrations=[]\n"),
        replace_in(tree, "set.conf", "license=MIT\n", 'license="MIT" # the set\'s licence\nrequires_capabilities=[]\n'))),
    ("acme.subagent-tools-scalar", lambda tree: replace_in(tree, "agents/acme-reviewer.md", "tools: [Read, Grep]", "tools: Read, Grep\nmaxTurns: 12")),
    ("acme.quoted-typed-scalars", lambda tree: (
        replace_in(tree, f"{SKILL}/SKILL.md", "ai-tools-libs: python/pdftext", 'ai-tools-libs: "true"\n  ai-tools-version: "1.0"'),
        replace_in(tree, f"{SKILL}/SKILL.md", "compatibility: Requires python3.", 'compatibility: "Requires: python3 # or newer"'))),
]


def symlink_fixture(tree: Tree) -> None:
    tree[f"{SKILL}/references/link.md"] = SymlinkTo("formats.md")


class SymlinkTo(str):
    """A tree entry written as a symbolic link to the target it names."""


FAIL_FIXTURES.append(("file.symlink", "fail", symlink_fixture))


def remove_under(tree: Tree, prefix: str) -> None:
    for path in [path for path in tree if path.startswith(prefix)]:
        del tree[path]


def inventory_lines(tree: Tree) -> str:
    """The `sha256sum` inventory of a tree, as build-set writes it."""
    import hashlib
    lines = []
    for path in sorted(tree):
        content = tree[path]
        data = content.encode("utf-8") if isinstance(content, str) else content
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path}\n")
    return "".join(lines)


def release_inventory_fixture(tree: Tree) -> None:
    tree["SHA256SUMS"] = "0" * 64 + "  set.conf\n"


def duplicate_inventory_fixture(tree: Tree) -> None:
    """A correct inventory with a second, all-zero line for set.conf ahead of the real one."""
    tree["SHA256SUMS"] = "0" * 64 + "  set.conf\n" + inventory_lines(tree)


# (fixture name, rule id, mutation) under the release profile.
RELEASE_FIXTURES: List[Tuple[str, str, Mutation]] = [
    ("release.inventory", "release.inventory", release_inventory_fixture),
    ("release.inventory.duplicate", "release.inventory", duplicate_inventory_fixture),
]

# (fixture name, rule id, mutation) validated with `--publisher-conf fixtures/publisher.conf`, which compares each
# manifest whole against its rendering.
PUBLISHER_CONF_FIXTURES: List[Tuple[str, str, Mutation]] = [
    ("set.manifest.plugin.author", "set.manifest.plugin",
     lambda tree: tree.__setitem__("plugin.json", tree["plugin.json"].replace("tools@acme.example", "someone@elsewhere.example"))),
]

PASS_FIXTURES: List[Tuple[str, str, str]] = [("acme", "acme", "acme"), ("core", "dag-node", "core")]
NAMED_SET_FIXTURES: List[Tuple[str, str, str, str]] = [
    # (fixture name, rule, publisher, set name): rules on the set name need a set named for them.
    ("name.reserved-word", "name.reserved-word", "openai-tools", "openai-tools"),
    ("name.publisher", "name.publisher", "beta", "acme"),
]


def fixture_conf(expect: str, rule: str, publisher: str, profile: str = "source", publisher_conf: bool = False) -> str:
    lines = [f"expect={expect}"]
    if rule:
        lines.append(f"rule={rule}")
    # The directory whose LICENSES/ holds the texts the fixtures share: fixtures/, two levels up from the fixture.
    lines.extend([f"publisher={publisher}", f"profile={profile}", "license_texts=../.."])
    if publisher_conf:
        lines.append("publisher_conf=../../publisher.conf")
    return "\n".join(lines) + "\n"


def write_tree(directory: pathlib.Path, tree: Tree) -> None:
    for relative_path, content in sorted(tree.items()):
        path = directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, SymlinkTo):
            os.symlink(str(content), path)
        elif isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def all_fixtures(destination: pathlib.Path) -> None:
    """Write every fixture under `destination`/pass and `destination`/fail, and the publisher.conf some are run with."""
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "publisher.conf").write_text(PUBLISHER_CONF.format(publisher="acme"), encoding="utf-8")
    for fixture_name, publisher, set_name in PASS_FIXTURES:
        directory = destination / "pass" / fixture_name
        (directory).mkdir(parents=True, exist_ok=True)
        (directory / "fixture.conf").write_text(fixture_conf("pass", "", publisher), encoding="utf-8")
        write_tree(directory / set_name, base_tree(publisher, set_name))
    for fixture_name, mutation in PASS_VARIANT_FIXTURES:
        directory = destination / "pass" / fixture_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fixture.conf").write_text(fixture_conf("pass", "", "acme"), encoding="utf-8")
        tree = base_tree("acme", "acme")
        mutation(tree)
        write_tree(directory / "acme", tree)
    fail_fixtures = [(rule, rule, expect, mutation) for rule, expect, mutation in FAIL_FIXTURES]
    fail_fixtures += [(name, rule, "fail", mutation) for name, rule, mutation in VARIANT_FIXTURES]
    for fixture_name, rule, expect, mutation in fail_fixtures:
        directory = destination / "fail" / fixture_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fixture.conf").write_text(fixture_conf(expect, rule, "acme"), encoding="utf-8")
        tree = base_tree("acme", "acme")
        mutation(tree)
        if not rule.startswith("set.manifest."):
            rerender_manifests(tree, "acme", "acme")
        write_tree(directory / "acme", tree)
    for fixture_name, rule, mutation in RELEASE_FIXTURES:
        directory = destination / "fail" / fixture_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fixture.conf").write_text(fixture_conf("fail", rule, "acme", profile="release"), encoding="utf-8")
        tree = base_tree("acme", "acme")
        mutation(tree)
        write_tree(directory / "acme", tree)
    for fixture_name, rule, mutation in PUBLISHER_CONF_FIXTURES:
        directory = destination / "fail" / fixture_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fixture.conf").write_text(fixture_conf("fail", rule, "acme", publisher_conf=True), encoding="utf-8")
        tree = base_tree("acme", "acme")
        mutation(tree)
        write_tree(directory / "acme", tree)
    for fixture_name, rule, publisher, set_name in NAMED_SET_FIXTURES:
        directory = destination / "fail" / fixture_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fixture.conf").write_text(fixture_conf("fail", rule, publisher), encoding="utf-8")
        write_tree(directory / set_name, base_tree(publisher, set_name))


def generate() -> int:
    for subdirectory in ("pass", "fail"):
        if (FIXTURES / subdirectory).exists():
            shutil.rmtree(FIXTURES / subdirectory)
    all_fixtures(FIXTURES)
    count = sum(1 for _ in (FIXTURES / "pass").iterdir()) + sum(1 for _ in (FIXTURES / "fail").iterdir())  # fixture directories
    print(f"fixture_generator: wrote {count} fixtures under {FIXTURES}")
    return 0


def check() -> int:
    with tempfile.TemporaryDirectory() as scratch:
        expected = pathlib.Path(scratch)
        all_fixtures(expected)
        differences = compare_trees(expected, FIXTURES, ("LICENSES",))
    for line in differences:
        print(f"fixture_generator: {line}", file=sys.stderr)
    if differences:
        print("fixture_generator: run `python3 tests/fixture_generator.py generate`", file=sys.stderr)
    return 1 if differences else 0


def compare_trees(expected: pathlib.Path, actual: pathlib.Path, ignore: Tuple[str, ...]) -> List[str]:
    comparison = filecmp.dircmp(expected, actual, ignore=list(ignore))
    differences: List[str] = []

    def walk(node: filecmp.dircmp, prefix: str) -> None:
        differences.extend(f"{prefix}{name}: only in the generator's output" for name in node.left_only)
        differences.extend(f"{prefix}{name}: not written by the generator" for name in node.right_only)
        differences.extend(f"{prefix}{name}: differs" for name in node.diff_files)
        differences.extend(f"{prefix}{name}: cannot be compared" for name in node.funny_files)
        for name, child in node.subdirs.items():
            walk(child, f"{prefix}{name}/")

    walk(comparison, "")
    return sorted(differences)


if __name__ == "__main__":
    verb = sys.argv[1] if len(sys.argv) > 1 else ""
    if verb == "generate":
        sys.exit(generate())
    if verb == "check":
        sys.exit(check())
    print(__doc__.strip().splitlines()[2].strip() + "\n" + __doc__.strip().splitlines()[3].strip(), file=sys.stderr)
    sys.exit(2)
