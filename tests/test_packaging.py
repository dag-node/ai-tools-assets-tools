# SPDX-License-Identifier: MIT
"""Drives packaging/render-nfpm.py over a set tools/build-set staged from the passing fixture, and over the tools RPM.

The workflow steps around it (the tag check, the signing, the containers) run on the GitHub runner alone and are proven
by a release; this file covers what a checkout can run: the rendered values, the refusals, and the quoting that keeps
a value from becoming configuration.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
FIXTURES = REPOSITORY / "fixtures"
TOOLS = REPOSITORY / "tools"
RENDER = REPOSITORY / "packaging" / "render-nfpm.py"
sys.path.insert(0, str(TESTS))

from fixture_generator import PUBLISHER_CONF  # noqa: E402


def run(script: pathlib.Path, *arguments: str):
    completed = subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


def contents(text: str) -> list:
    """The rendered `contents:` entries as (dst, type) pairs, in order; an entry without `type:` is a file."""
    entries, inside = [], False
    for line in text.splitlines():
        if line == "contents:":
            inside = True
        elif inside and not line.startswith(" "):
            break
        elif inside and line.lstrip().startswith("- "):
            entries.append({})
        if inside and entries:
            key, separator, value = line.strip().removeprefix("- ").partition(": ")
            if separator and key in ("dst", "type"):
                entries[-1][key] = json.loads(value) if value.startswith('"') else value
    return [(entry["dst"], entry.get("type", "file")) for entry in entries]


class TemplateRules:
    """nFPM refuses a second entry at a tree's destination, since the tree writes that directory itself."""

    def assert_no_entry_at_a_tree_destination(self, text: str):
        entries = contents(text)
        trees = [dst for dst, kind in entries if kind == "tree"]
        self.assertTrue(trees)
        for tree in trees:
            self.assertEqual([dst for dst, _ in entries].count(tree), 1, f"{tree} has an entry beside its tree")


class RenderSetConfiguration(TemplateRules, unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "acme-assets"
        (self.root / "sets").mkdir(parents=True)
        shutil.copytree(FIXTURES / "pass" / "acme" / "acme", self.root / "sets" / "acme")
        shutil.copytree(FIXTURES / "LICENSES", self.root / "LICENSES")
        (self.root / "LICENSE").write_text((FIXTURES / "LICENSES" / "MIT.txt").read_text(encoding="utf-8"), encoding="utf-8")
        (self.root / "publisher.conf").write_text(PUBLISHER_CONF.format(publisher="acme"), encoding="utf-8")
        for command in (("sync-manifests",), ("build-set", "acme")):
            status, _, stderr = run(TOOLS / command[0], *command[1:], "--root", str(self.root))
            self.assertEqual(status, 0, stderr)
        self.staged = self.root / "build" / "acme"
        self.output = self.root / "build" / "nfpm.yaml"

    def tearDown(self):
        shutil.rmtree(self.root.parent)

    def render(self, version="0.1.0", set_name="acme", dist="el9"):
        return run(RENDER, "set", "--root", str(self.root), "--set", set_name, "--version", version,
                   "--staged", str(self.staged), "--dist", dist, "--output", str(self.output))

    def rendered(self) -> dict:
        """The rendered top-level `key: value` lines whose value is a JSON string, by key."""
        values = {}
        for line in self.output.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(": ")
            if separator and not line.startswith((" ", "#")) and value.startswith('"'):
                values[key] = json.loads(value)
        return values

    def test_the_staged_set_renders_its_package(self):
        status, _, stderr = self.render()
        self.assertEqual(status, 0, stderr)
        text = self.output.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"@[A-Z_]+@")
        self.assertEqual(self.rendered(), {
            "name": "ai-tools-assets-acme",
            "version": "0.1.0",
            "release": "1.el9",
            "maintainer": "acme <tools@acme.example>",
            "vendor": "acme",
            "homepage": "https://github.com/acme/ai-tools-assets",
            "license": "MIT",
            "description": "Fixture skills and subagents",
        })
        self.assertIn(f"src: {json.dumps(str(self.staged.resolve()))}", text)
        self.assertIn('dst: "/usr/share/ai-tools-assets/acme"', text)
        self.assert_no_entry_at_a_tree_destination(text)
        self.assertIn(("/usr/share/ai-tools-assets", "dir"), contents(text))

    def test_a_version_other_than_the_tags_is_refused(self):
        status, _, stderr = self.render(version="0.2.0")
        self.assertEqual(status, 1)
        self.assertIn("not the tag's `0.2.0`", stderr)
        self.assertFalse(self.output.exists())

    def test_each_dist_tag_names_the_release(self):
        for dist in ("el10", "fc44"):
            status, _, stderr = self.render(dist=dist)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(self.rendered()["release"], f"1.{dist}")

    def test_a_dist_that_is_not_a_dist_tag_is_refused(self):
        for dist in ("", "el", "el09", "noarch", "el9.x", "fc44\n"):
            status, _, stderr = self.render(dist=dist)
            self.assertEqual(status, 1, dist)
            self.assertIn("is not a dist tag", stderr)
            self.assertFalse(self.output.exists())

    def test_a_set_other_than_the_tags_is_refused(self):
        status, _, stderr = self.render(set_name="other")
        self.assertEqual(status, 1)
        self.assertIn("not the set `other`", stderr)

    def test_a_value_stays_one_quoted_scalar(self):
        set_conf = self.staged / "set.conf"
        hostile = 'He said "hi": @VERSION@\\n  - src: /etc'
        set_conf.write_text(set_conf.read_text(encoding="utf-8").replace(
            'summary="Fixture skills and subagents"', f"summary='{hostile}'"), encoding="utf-8")
        status, _, stderr = self.render()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.rendered()["description"], hostile)
        self.assertNotIn("/etc", [line.strip().removeprefix("- src: ") for line in self.output.read_text(encoding="utf-8").splitlines()])

    def test_a_set_conf_the_reader_reports_is_refused(self):
        set_conf = self.staged / "set.conf"
        set_conf.write_text(set_conf.read_text(encoding="utf-8") + "version=0.1.0\n", encoding="utf-8")
        status, _, stderr = self.render()
        self.assertEqual(status, 1)
        self.assertIn("`version` is given twice", stderr)


class RenderToolsConfiguration(TemplateRules, unittest.TestCase):
    def setUp(self):
        self.directory = pathlib.Path(tempfile.mkdtemp())
        self.output = self.directory / "nfpm.yaml"

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_the_tag_version_renders_and_every_installed_path_is_in_the_tree(self):
        status, _, stderr = run(RENDER, "tools", "--version", "1.0.0-rc.1", "--dist", "fc44",
                                "--output", str(self.output))
        self.assertEqual(status, 0, stderr)
        text = self.output.read_text(encoding="utf-8")
        self.assertIn('version: "1.0.0-rc.1"', text)
        self.assertIn('release: "1.fc44"', text)
        self.assertNotRegex(text, r"@[A-Z_]+@")
        sources = [line.split("src: ", 1)[1] for line in text.splitlines() if line.strip().startswith("- src: ")]
        self.assertEqual(sources, ["tools", "format", "fixtures", "formatters", "LICENSES", "LICENSE"])
        for source in sources:
            self.assertTrue((REPOSITORY / source).exists(), source)
        self.assert_no_entry_at_a_tree_destination(text)

    def test_a_version_that_is_not_semantic_is_refused(self):
        status, _, stderr = run(RENDER, "tools", "--version", "1.0", "--dist", "el9", "--output", str(self.output))
        self.assertEqual(status, 1)
        self.assertIn("not a semantic version", stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
