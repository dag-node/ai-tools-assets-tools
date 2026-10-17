# SPDX-License-Identifier: MIT
"""Drives each command over a publisher repository built from the passing fixture in a temporary directory.

The repository-level rules (publisher.conf, the marketplace, the stale-manifest check), the scaffolding commands, the
builder's inventory and zip, link-set's placement and removal, check-licenses over a tree and check-signoff over a
synthetic history each have a case here; the set-level rules are tests/test_fixtures.py's.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
FIXTURES = REPOSITORY / "fixtures"
TOOLS = REPOSITORY / "tools"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TOOLS / "lib"))

from fixture_generator import PUBLISHER_CONF  # noqa: E402


def run(command: str, *arguments: str, cwd=None, env=None):
    completed = subprocess.run([sys.executable, str(TOOLS / command), *arguments], capture_output=True, text=True, cwd=cwd,
                               env=env)
    return completed.returncode, completed.stdout, completed.stderr


def compose_publisher(root: pathlib.Path) -> None:
    """A publisher repository at `root` composed from fixtures/pass/acme."""
    (root / "sets").mkdir(parents=True)
    shutil.copytree(FIXTURES / "pass" / "acme" / "acme", root / "sets" / "acme")
    shutil.copytree(FIXTURES / "LICENSES", root / "LICENSES")
    (root / "LICENSE").write_text((FIXTURES / "LICENSES" / "MIT.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "publisher.conf").write_text(PUBLISHER_CONF.format(publisher="acme"), encoding="utf-8")
    status, _, stderr = run("sync-manifests", "--root", str(root))
    assert status == 0, stderr


def load_command(command: str):
    """Import `tools/<command>`, a script without the .py suffix, as a module."""
    name = command.replace("-", "_") + "_command"
    loader = importlib.machinery.SourceFileLoader(name, str(TOOLS / command))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def gpg_runs() -> bool:
    return shutil.which("gpg") is not None and shutil.which("gpgv") is not None


class PublisherRepository(unittest.TestCase):
    """A publisher repository composed from fixtures/pass/acme."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "acme-assets"
        compose_publisher(self.root)

    def tearDown(self):
        shutil.rmtree(self.root.parent)

    def test_the_composed_repository_validates(self):
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""))
        status, _, stderr = run("sync-manifests", "--root", str(self.root), "--check")
        self.assertEqual((status, stderr), (0, ""))

    def test_a_stale_marketplace_is_refused(self):
        marketplace = self.root / ".claude-plugin" / "marketplace.json"
        marketplace.write_text(marketplace.read_text(encoding="utf-8").replace("acme-ai-tools-assets", "other"), encoding="utf-8")
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("marketplace.json", stderr)
        status, _, stderr = run("sync-manifests", "--root", str(self.root), "--check")
        self.assertEqual(status, 1)
        self.assertIn("repo.marketplace", stderr)

    def test_a_root_licence_text_with_its_holder_filled_warns_in_validate_and_build_set(self):
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""), "the published texts pass")
        text = self.root / "LICENSES" / "MIT.txt"
        text.write_text(text.read_text(encoding="utf-8").replace("<year> <copyright holders>", "2026 Acme"), encoding="utf-8")
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        self.assertIn("validate: warning: LICENSES/MIT.txt: license.text-modified: is not the `MIT` text as published", stderr)
        status, _, stderr = run("build-set", "acme", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        self.assertIn("acme/LICENSES/MIT.txt: license.text-modified: is not the `MIT` text as published", stderr,
                      "the staged copy is held to it too")

    def test_a_publisher_conf_without_its_keys_is_refused(self):
        (self.root / "publisher.conf").write_text("publisher=acme\n", encoding="utf-8")
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("repo.publisher-conf", stderr)

    def test_a_publisher_conf_key_outside_its_table_is_refused_and_an_extension_key_is_not(self):
        conf = self.root / "publisher.conf"
        original = conf.read_text(encoding="utf-8")
        conf.write_text(original + "x_review=quarterly\n", encoding="utf-8")
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""))
        # A misspelt setting would otherwise read as absent, which is the permissive default.
        conf.write_text(original + "allow_dynamic_injectoin=yes\n", encoding="utf-8")
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("repo.publisher-conf: `allow_dynamic_injectoin` is not a key this format reads", stderr)

    def test_a_directory_that_is_no_repository_is_refused(self):
        status, _, stderr = run("validate", "--root", str(self.root / "sets"))
        self.assertEqual(status, 1)
        self.assertIn("repo.layout", stderr)

    def test_a_set_selector_is_a_name_matched_against_the_discovered_sets(self):
        (self.root / "sets" / "acme" / "skills" / "acme-pdf-processing" / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
        for selector, rule in (("..", "name.grammar"), (str(self.root / "sets" / "acme"), "name.grammar"), ("nope", "set.conf.missing")):
            status, _, stderr = run("validate", "--root", str(self.root), "--set", selector)
            self.assertEqual(status, 1, selector)
            self.assertIn(rule, stderr)
            self.assertNotIn("frontmatter.missing", stderr, "no set was validated for a selector that names none")
        status, _, stderr = run("validate", "--root", str(self.root), "--set", "acme")
        self.assertEqual(status, 1)
        self.assertIn("frontmatter.missing", stderr)

    def test_a_linked_repository_licence_text_is_not_counted(self):
        outside = self.root.parent / "MIT.txt"
        text = self.root / "LICENSES" / "MIT.txt"
        shutil.move(str(text), str(outside))
        text.symlink_to(outside)
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("sets/acme/set.conf: license.text: no `MIT.txt`", stderr)
        text.unlink()
        os.link(outside, text)
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1, "a text with a second hard link is not counted either")
        self.assertIn("license.text", stderr)
        text.unlink()
        shutil.move(str(outside), str(text))
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""))

    def test_the_publisher_checks_read_the_shared_key_value_grammar(self):
        set_conf = self.root / "sets" / "acme" / "set.conf"
        text = set_conf.read_text(encoding="utf-8")
        text = text.replace("source=https://github.com/acme/ai-tools-assets", 'source="https://github.com/acme/ai-tools-assets" # the publisher\'s')
        text = text.replace("maintainers=[maintainers@acme.example]", "maintainers=maintainers@acme.example")
        set_conf.write_text(text, encoding="utf-8")
        status, _, stderr = run("check-publisher", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""))
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""))
        set_conf.write_text(text.replace("maintainers=maintainers@acme.example", "maintainers=[a,,b]"), encoding="utf-8")
        status, _, stderr = run("check-publisher", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("`maintainers` is not a list", stderr)

    def test_a_symlinked_manifest_is_refused_before_any_publisher_check_reads_it(self):
        outside = self.root.parent / "outside.json"
        manifest = self.root / "sets" / "acme" / "plugin.json"
        outside.write_text(manifest.read_text(encoding="utf-8").replace("acme", "other"), encoding="utf-8")
        manifest.unlink()
        manifest.symlink_to(outside)
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("sets/acme/plugin.json: file.symlink: ", stderr)
        self.assertNotIn("check-publisher:", stderr, "the publisher check does not run over a set the walk refused")
        status, _, stderr = run("check-publisher", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("plugin.json: is a symbolic link", stderr)
        self.assertNotIn("other", stderr, "the link's target is not read")

    def check_publisher_in_process(self, after_set_conf_read=None):
        """check-publisher's `check` run in this process; its status, its findings and how often it read set.conf."""
        import safe_read
        command = load_command("check-publisher")
        command.ROOT = self.root
        command.ROOT_FD = safe_read.open_root(self.root)
        reads = []
        read_text = command.read_text

        def counted(relative_path):
            text = read_text(relative_path)
            if pathlib.PurePath(relative_path) == pathlib.PurePath("sets/acme/set.conf"):
                reads.append(relative_path)
                if after_set_conf_read is not None:
                    after_set_conf_read()
            return text

        try:
            with mock.patch.object(command, "read_text", side_effect=counted), contextlib.redirect_stderr(io.StringIO()):
                status = command.check(command.reserved_words(), None)
        finally:
            os.close(command.ROOT_FD)
        return status, command.findings, len(reads)

    def test_the_publisher_check_holds_the_set_conf_it_discovered(self):
        self.assertEqual(self.check_publisher_in_process(), (0, [], 1), "control: the unchanged set, its set.conf read once")
        set_conf = self.root / "sets" / "acme" / "set.conf"
        set_conf.write_text(set_conf.read_text(encoding="utf-8").replace("source=https://github.com/acme/ai-tools-assets",
                                                                         "source=https://example.org/other"), encoding="utf-8")
        status, findings, reads = self.check_publisher_in_process(after_set_conf_read=set_conf.unlink)
        self.assertEqual((status, reads), (1, 1), "a set.conf removed after discovery is checked from the text discovered")
        self.assertIn("check-publisher: sets/acme/set.conf: `source` is `https://example.org/other`, expected "
                      "`https://github.com/acme/ai-tools-assets` from publisher.conf", findings)
        (self.root.parent / "set.conf").write_text("name=acme\n", encoding="utf-8")
        set_conf.symlink_to(self.root.parent / "set.conf")
        status, _, stderr = run("check-publisher", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("check-publisher: sets/acme/set.conf: is a symbolic link", stderr, "a link at discovery keeps its refusal")

    def test_a_symlinked_sets_directory_and_set_are_refused(self):
        real = self.root.parent / "elsewhere"
        shutil.move(str(self.root / "sets"), str(real))
        (self.root / "sets").symlink_to(real, target_is_directory=True)
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("sets: repo.layout: is a symbolic link", stderr)
        (self.root / "sets").unlink()
        (self.root / "sets").mkdir()
        (self.root / "sets" / "acme").symlink_to(real / "acme", target_is_directory=True)
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("sets/acme: file.symlink: ", stderr)
        self.assertNotIn("check-publisher:", stderr)

    def test_scaffolding_a_set_and_its_assets_validates(self):
        status, stdout, stderr = run("new-set", "acme-dotnet", "--root", str(self.root), "--summary", "ASP.NET Core skills")
        self.assertEqual(status, 0, stderr)
        self.assertIn("sets/acme-dotnet/set.conf", stdout)
        readme = (self.root / "sets" / "acme-dotnet" / "README.md").read_text(encoding="utf-8")
        self.assertIn("\n\nASP.NET Core skills\n\n", readme, "the README holds the summary as given, not set.conf's quoting")
        self.assertIn("dnf package `ai-tools-assets-sets-acme-dotnet`", readme)
        self.assertIn("plugin\n`ai-tools-assets-acme-dotnet`", readme)
        self.assertIn("dnf package `ai-tools-assets-sets-acme-dotnet`",
                      (self.root / "sets" / "acme-dotnet" / "CHANGELOG.md").read_text(encoding="utf-8"))
        status, _, stderr = run("new-asset", "acme-dotnet", "skill", "acme-dotnet-ef-migrations", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        status, _, stderr = run("new-asset", "acme-dotnet", "subagent", "acme-dotnet-reviewer", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""))

    def test_scaffolding_quotes_a_description_and_a_summary_and_refuses_an_empty_one(self):
        for index, description in enumerate(("true", "a: b", 'say "hi"', "back\\slash # not a comment", "2026-10-06", "1:20")):
            for kind, name in (("skill", f"acme-s{index}"), ("subagent", f"acme-a{index}")):
                status, _, stderr = run("new-asset", "acme", kind, name, "--root", str(self.root), "--description", description)
                self.assertEqual(status, 0, (description, stderr))
        self.assertIn('description: "say \\"hi\\""', (self.root / "sets" / "acme" / "agents" / "acme-a2.md").read_text(encoding="utf-8"))
        status, _, stderr = run("new-asset", "acme", "skill", "acme-empty", "--root", str(self.root), "--description", " ")
        self.assertEqual(status, 1)
        self.assertIn("frontmatter.required", stderr)
        self.assertFalse((self.root / "sets" / "acme" / "skills" / "acme-empty").exists())
        status, _, stderr = run("new-set", "acme-quoted", "--root", str(self.root), "--summary", 'say "hi" # loudly')
        self.assertEqual(status, 0, stderr)
        self.assertIn("summary='say \"hi\" # loudly'\n", (self.root / "sets" / "acme-quoted" / "set.conf").read_text(encoding="utf-8"))
        for summary, rule in (("it's \"both\"", "set.conf.syntax"), (" ", "set.conf.required-key")):
            status, _, stderr = run("new-set", "acme-refused", "--root", str(self.root), "--summary", summary)
            self.assertEqual(status, 1, summary)
            self.assertIn(rule, stderr)
            self.assertFalse((self.root / "sets" / "acme-refused").exists())
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""), "every template written validates as written")

    def test_scaffolding_does_not_write_through_a_link(self):
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        # A dangling link at the asset's own name.
        (self.root / "sets" / "acme" / "agents" / "acme-new.md").symlink_to(elsewhere / "written.md")
        status, _, stderr = run("new-asset", "acme", "subagent", "acme-new", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("kind.shape", stderr)
        self.assertFalse((elsewhere / "written.md").exists())
        # A linked kind directory.
        shutil.move(str(self.root / "sets" / "acme" / "skills"), str(elsewhere / "skills"))
        (self.root / "sets" / "acme" / "skills").symlink_to(elsewhere / "skills", target_is_directory=True)
        status, _, stderr = run("new-asset", "acme", "skill", "acme-new", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("file.symlink", stderr)
        self.assertFalse((elsewhere / "skills" / "acme-new").exists())
        # A linked sets/ directory, for new-set and for sync-manifests.
        shutil.move(str(self.root / "sets"), str(elsewhere / "sets"))
        (self.root / "sets").symlink_to(elsewhere / "sets", target_is_directory=True)
        status, _, stderr = run("new-set", "acme-dotnet", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("file.symlink", stderr)
        self.assertFalse((elsewhere / "sets" / "acme-dotnet").exists())
        status, _, stderr = run("sync-manifests", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("sets: repo.layout: is a symbolic link", stderr)
        # A generated manifest replaced by a link is reported and left, not written through.
        (self.root / "sets").unlink()
        shutil.move(str(elsewhere / "sets"), str(self.root / "sets"))
        marketplace = self.root / ".claude-plugin" / "marketplace.json"
        marketplace.unlink()
        marketplace.symlink_to(elsewhere / "marketplace.json")
        status, _, stderr = run("sync-manifests", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("marketplace.json: file.symlink", stderr)
        self.assertFalse((elsewhere / "marketplace.json").exists())

    def test_a_hard_linked_generated_file_is_refused_and_its_other_name_is_not_written_into(self):
        outside = self.root.parent / "outside.json"
        outside.write_text("OUTSIDE DATA\n", encoding="utf-8")
        manifest = self.root / "sets" / "acme" / "plugin.json"
        manifest.unlink()
        os.link(outside, manifest)
        status, _, stderr = run("sync-manifests", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("sets/acme/plugin.json: file.hardlink: ", stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "OUTSIDE DATA\n")
        manifest.unlink()
        status, _, stderr = run("sync-manifests", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        status, _, stderr = run("build-set", "acme", cwd=self.root)
        self.assertEqual(status, 0, stderr)
        marker = self.root / "build" / ".acme.ai-tools-assets-build"
        archive = self.root / "dist" / "ai-tools-assets-sets-acme-0.1.0.zip"
        # A hard-linked marker reads as no marker, so the build refuses before it would replace the marker.
        for generated, content, refusal in ((marker, marker.read_bytes(), "repo.layout: exists and is not a build of `acme`"),
                                            (archive, b"OUTSIDE ZIP", "file.hardlink: ")):
            other_name = self.root.parent / ("outside-" + generated.name)
            other_name.write_bytes(content)
            generated.unlink()
            os.link(other_name, generated)
            status, _, stderr = run("build-set", "acme", cwd=self.root)
            self.assertEqual(status, 1, generated.name)
            self.assertIn(refusal, stderr)
            self.assertEqual(other_name.read_bytes(), content, "the inode behind the other name is unchanged")
            generated.unlink()
            other_name.unlink()
            generated.write_bytes(content)  # one link again: the entry is replaced
            status, _, stderr = run("build-set", "acme", cwd=self.root)
            self.assertEqual(status, 0, stderr)
        self.assertEqual(sorted(path.name for path in (self.root / "build").iterdir()), [".acme.ai-tools-assets-build", "acme"],
                         "no temporary name is left beside the replaced entry")

    def test_scaffolding_refuses_a_bad_name(self):
        status, _, stderr = run("new-set", "openai-things", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("name.reserved-word", stderr)
        self.assertFalse((self.root / "sets" / "openai-things").exists())
        status, _, stderr = run("new-set", "beta", "--root", str(self.root))
        self.assertIn("name.publisher", stderr)
        status, _, stderr = run("new-asset", "acme", "skill", "ef-migrations", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("name.asset-prefix", stderr)

    def test_build_set_stages_inventories_and_zips(self):
        status, stdout, stderr = run("build-set", "acme", cwd=self.root)
        self.assertEqual(status, 0, stderr)
        staged = self.root / "build" / "acme"
        self.assertTrue((staged / "LICENSES" / "MIT.txt").is_file())
        self.assertTrue((staged / "LICENSES" / "CC0-1.0.txt").is_file(), "the vendored assets' licence text is carried")
        self.assertTrue((staged / "LICENSE").is_file())
        inventory = (staged / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        listed = {line.split("  ", 1)[1] for line in inventory}
        present = {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file() and path.name != "SHA256SUMS"}
        self.assertEqual(listed, present)
        for line in inventory:
            digest, relative = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((staged / relative).read_bytes()).hexdigest(), digest)
        archive = self.root / "dist" / "ai-tools-assets-sets-acme-0.1.0.zip"
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as opened:
            names = set(opened.namelist())
        self.assertIn("plugin.json", names, "the plugin root is at the top of the archive")
        self.assertIn("SHA256SUMS", names)
        self.assertEqual((self.root / "dist" / "ai-tools-assets-sets-acme-0.1.0.zip.sha256").read_text(encoding="utf-8").split()[0],
                         hashlib.sha256(archive.read_bytes()).hexdigest())
        status, _, stderr = run("validate", "--set-directory", str(staged), "--publisher", "acme", "--profile", "release")
        self.assertEqual((status, stderr), (0, ""))
        first = archive.read_bytes()
        status, _, _ = run("build-set", "acme", cwd=self.root)
        self.assertEqual(first, archive.read_bytes(), "the same tree gives the same zip")

    def test_build_set_refuses_a_set_that_does_not_validate(self):
        (self.root / "sets" / "acme" / "notes.txt").write_text("stray\n", encoding="utf-8")
        status, _, stderr = run("build-set", "acme", cwd=self.root)
        self.assertEqual(status, 1)
        self.assertIn("set.entry.unknown", stderr)
        self.assertFalse((self.root / "build").exists())

    def assert_source_intact(self):
        self.assertTrue((self.root / "sets" / "acme" / "set.conf").is_file())
        self.assertEqual([path.name for path in self.root.glob(".acme.*")] + [path.name for path in (self.root / "sets").glob(".acme.*")], [])

    def test_build_set_refuses_a_path_as_the_set_and_an_output_root_over_the_source(self):
        status, _, stderr = run("build-set", str(self.root / "sets" / "acme"), cwd=self.root)
        self.assertEqual(status, 1)
        self.assertIn("name.grammar", stderr)
        for option, value in (("--build", "sets"), ("--dist", "sets/acme"), ("--build", "."), ("--dist", str(self.root.parent))):
            status, _, stderr = run("build-set", "acme", option, value, cwd=self.root)
            self.assertEqual(status, 1, (option, value))
            self.assertIn("repo.layout", stderr)
            self.assert_source_intact()
        (self.root / "real-build").mkdir()
        (self.root / "linked-build").symlink_to(self.root / "real-build", target_is_directory=True)
        status, _, stderr = run("build-set", "acme", "--build", "linked-build", cwd=self.root)
        self.assertEqual(status, 1)
        self.assertIn("is a symbolic link", stderr)
        self.assertEqual(list((self.root / "real-build").iterdir()), [])

    def test_build_set_replaces_a_build_of_this_set_alone(self):
        destination = self.root / "build" / "acme"
        marker = self.root / "build" / ".acme.ai-tools-assets-build"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_text("not a build\n", encoding="utf-8")
        for marker_text in (None, "ai-tools-assets-build 1\nset=other\n", "ai-tools-assets-build 2\nset=acme\n"):
            if marker_text is not None:
                marker.write_text(marker_text, encoding="utf-8")
            status, _, stderr = run("build-set", "acme", cwd=self.root)
            self.assertEqual(status, 1, marker_text)
            self.assertIn("repo.layout", stderr)
            self.assertTrue((destination / "keep.txt").is_file(), "the directory at the destination is left")
            self.assertEqual([path.name for path in (self.root / "build").iterdir() if path.name.startswith(".acme.") and path.is_dir()], [],
                             "the staging directory is removed on failure")
        marker.write_text("ai-tools-assets-build 1\nset=acme\n", encoding="utf-8")
        status, _, stderr = run("build-set", "acme", cwd=self.root)
        self.assertEqual(status, 0, stderr)
        self.assertFalse((destination / "keep.txt").exists())
        self.assertTrue((destination / "SHA256SUMS").is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "ai-tools-assets-build 1\nset=acme\n")
        self.assertEqual(sorted(path.name for path in (self.root / "build").iterdir()), [".acme.ai-tools-assets-build", "acme"])

    def build_in_process(self, build_set):
        """build-set's `build` run in this process, so a test can patch a step of it; its status."""
        import safe_read
        from findings import FindingCollector
        root_fd = safe_read.open_root(self.root)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return build_set.build(root_fd, self.root, "acme", pathlib.Path("build"), pathlib.Path("dist"), FindingCollector("build-set"))
        finally:
            os.close(root_fd)

    def test_a_build_root_swapped_for_a_link_after_the_marker_check_removes_and_writes_nothing_outside(self):
        self.assertEqual(run("build-set", "acme", cwd=self.root)[0], 0)
        outside = self.root.parent / "outside"
        (outside / "acme").mkdir(parents=True)
        (outside / "acme" / "keep").write_text("theirs\n", encoding="utf-8")
        build_set = load_command("build-set")
        checked = build_set.is_managed_build_directory

        def swap(*arguments):
            answer = checked(*arguments)
            (self.root / "build").rename(self.root / "build-checked")
            (self.root / "build").symlink_to(outside, target_is_directory=True)
            return answer

        with mock.patch.object(build_set, "is_managed_build_directory", side_effect=swap):
            status = self.build_in_process(build_set)
        self.assertEqual((outside / "acme" / "keep").read_text(encoding="utf-8"), "theirs\n", "the outside tree is not removed")
        self.assertEqual(sorted(path.relative_to(outside).as_posix() for path in outside.rglob("*")), ["acme", "acme/keep"],
                         "and nothing is written into it")
        self.assertEqual(status, 0)
        self.assertTrue((self.root / "build-checked" / "acme" / "SHA256SUMS").is_file(), "the build replaces the stage the check read")
        (self.root / "build").unlink()
        (self.root / "build-checked").rename(self.root / "build")
        (self.root / "build" / "acme" / "stale.txt").write_text("from the previous build\n", encoding="utf-8")
        self.assertEqual(self.build_in_process(build_set), 0, "control: the same build root, unchanged")
        self.assertFalse((self.root / "build" / "acme" / "stale.txt").exists(), "the managed stage is replaced")

    def managed(self):
        import staged_build
        build_fd = os.open(self.root / "build", os.O_RDONLY | os.O_DIRECTORY)
        try:
            return staged_build.is_managed_build_directory(build_fd, "acme")
        finally:
            os.close(build_fd)

    def test_a_marker_the_reader_refuses_reads_as_no_marker(self):
        self.assertEqual(run("build-set", "acme", cwd=self.root)[0], 0)
        marker = self.root / "build" / ".acme.ai-tools-assets-build"
        text = marker.read_bytes()
        self.assertTrue(self.managed(), "control: the regular marker build-set wrote")
        other = self.root.parent / "outside-marker"
        other.write_bytes(text)
        marker.unlink()
        marker.symlink_to(other)
        self.assertFalse(self.managed(), "a link")
        status, _, stderr = run("sign-set", "acme", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("repo.layout: is not a set build-set staged", stderr)
        marker.unlink()
        os.link(other, marker)
        self.assertFalse(self.managed(), "a second hard link")
        marker.unlink()
        marker.write_bytes(text + b"set=other\n")
        self.assertFalse(self.managed(), "a marker longer than its own text")
        marker.write_bytes(text)
        self.assertTrue(self.managed(), "control: the marker restored")
        stage = self.root / "build" / "acme"
        stage.rename(self.root / "build" / "acme-real")
        stage.symlink_to(self.root / "build" / "acme-real", target_is_directory=True)
        self.assertFalse(self.managed(), "a link at the stage")

    def test_a_marker_that_is_a_fifo_reads_as_no_marker_without_waiting_on_a_writer(self):
        self.assertEqual(run("build-set", "acme", cwd=self.root)[0], 0)
        marker = self.root / "build" / ".acme.ai-tools-assets-build"
        marker.unlink()
        try:
            os.mkfifo(marker)
        except OSError as error:
            self.skipTest(f"this host denies creating a FIFO: {error.strerror}")
        completed = subprocess.run([sys.executable, str(TOOLS / "sign-set"), "acme", "--root", str(self.root)],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("repo.layout: is not a set build-set staged", completed.stderr)

    def test_link_set_places_records_and_removes(self):
        target = self.root.parent / "home" / ".claude" / "skills"
        status, stdout, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target))
        self.assertEqual(status, 0, stderr)
        self.assertTrue((target / "acme-pdf-processing").is_symlink())
        self.assertTrue((target / "pdftext").is_symlink())
        self.assertFalse((target / "acme-reviewer").exists(), "a subagent is never placed in a skills directory")
        (target / "other").mkdir()
        shutil.rmtree(target / "pdftext") if (target / "pdftext").is_dir() and not (target / "pdftext").is_symlink() else (target / "pdftext").unlink()
        (target / "pdftext").mkdir()
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target))
        self.assertEqual(status, 1, "an entry this command did not place is refused")
        self.assertIn("name.collision", stderr)
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--remove")
        self.assertEqual(status, 1, stderr)
        self.assertFalse((target / "acme-pdf-processing").exists())
        self.assertTrue((target / "other").is_dir(), "an entry the command did not place is left")
        status, _, _ = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--copy", "--only", "acme-pdf-processing")
        self.assertEqual(status, 0)
        self.assertTrue((target / "acme-pdf-processing" / "SKILL.md").is_file())
        self.assertFalse((target / "acme-pdf-processing").is_symlink())
        record = (target / ".ai-tools-assets-acme.links").read_text(encoding="utf-8").splitlines()
        copied = [line for line in record if line.startswith("acme-pdf-processing\t")]
        name, mode, identity = copied[0].split("\t")
        self.assertEqual((name, mode, len(identity)), ("acme-pdf-processing", "copy", 64))
        self.assertIn("pdftext\tlink\t", "\n".join(record), "the entry the removal left stays recorded")

    def test_link_set_validates_the_set_before_placing_and_follows_no_link(self):
        target = self.root.parent / "home" / ".claude" / "skills"
        outside = self.root.parent / "leak.txt"
        outside.write_text("OUTSIDE DATA\n", encoding="utf-8")
        leak = self.root / "sets" / "acme" / "skills" / "acme-pdf-processing" / "references" / "leak.txt"
        leak.symlink_to(outside)
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--copy")
        self.assertEqual(status, 1)
        self.assertIn("references/leak.txt: file.symlink: ", stderr)
        self.assertNotIn("OUTSIDE", stderr, "the link's target is not read")
        self.assertFalse(target.exists(), "nothing is placed from a set that does not validate")
        leak.unlink()
        (self.root / "sets" / "acme" / "notes.txt").write_text("stray\n", encoding="utf-8")
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target))
        self.assertEqual(status, 1)
        self.assertIn("set.entry.unknown", stderr)
        self.assertFalse(target.exists())
        (self.root / "sets" / "acme" / "notes.txt").unlink()
        real = self.root.parent / "elsewhere"
        shutil.move(str(self.root / "sets"), str(real))
        (self.root / "sets").symlink_to(real, target_is_directory=True)
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target))
        self.assertEqual(status, 1)
        self.assertIn("is a symbolic link", stderr)
        self.assertFalse(target.exists())

    def test_link_set_removes_what_still_matches_its_record_alone(self):
        target = self.root.parent / "home" / ".claude" / "skills"
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target))
        self.assertEqual(status, 0, stderr)
        record = target / ".ai-tools-assets-acme.links"
        lines = record.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split("\t")[:2] for line in lines], [["acme-pdf-processing", "link"], ["pdftext", "link"]])
        self.assertEqual(lines[1].split("\t")[2], str(self.root / "sets" / "acme" / "skills" / "pdftext"))
        # A replaced entry: a real directory where the link was, and a link retargeted elsewhere.
        (target / "acme-pdf-processing").unlink()
        (target / "acme-pdf-processing").mkdir()
        (target / "acme-pdf-processing" / "SKILL.md").write_text("mine\n", encoding="utf-8")
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        (target / "pdftext").unlink()
        (target / "pdftext").symlink_to(elsewhere, target_is_directory=True)
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--remove")
        self.assertEqual(status, 1)
        self.assertEqual(stderr.count("name.collision"), 2, stderr)
        self.assertTrue((target / "acme-pdf-processing" / "SKILL.md").is_file(), "the replacement is left")
        self.assertTrue((target / "pdftext").is_symlink() and elsewhere.is_dir(), "the retargeted link and its target are left")
        self.assertEqual(len(record.read_text(encoding="utf-8").splitlines()), 2, "the entries stay recorded")

    def test_link_set_refuses_a_tampered_record_and_leaves_a_modified_copy(self):
        target = self.root.parent / "home" / ".claude" / "skills"
        unrelated = self.root.parent / "home" / ".claude" / "unrelated"
        unrelated.mkdir(parents=True)
        target.mkdir()
        (unrelated / "SKILL.md").write_text("mine\n", encoding="utf-8")
        record = target / ".ai-tools-assets-acme.links"
        for line in ("../unrelated\tcopy\n", "../unrelated\tcopy\tx\n", "pdftext\tmove\tx\n", "pdftext\tlink\n"):
            record.write_text(line, encoding="utf-8")
            status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--remove")
            self.assertEqual(status, 1, line)
            self.assertIn("refused whole", stderr)
            self.assertTrue((unrelated / "SKILL.md").is_file())
        record.unlink()
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--copy", "--only", "pdftext")
        self.assertEqual(status, 0, stderr)
        (target / "pdftext" / "SKILL.md").write_text("edited\n", encoding="utf-8")
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--remove")
        self.assertEqual(status, 1)
        self.assertIn("pdftext: name.collision: is not the copy", stderr)
        self.assertEqual((target / "pdftext" / "SKILL.md").read_text(encoding="utf-8"), "edited\n")
        status, _, stderr = run("link-set", "..", "--root", str(self.root), "--target", str(target))
        self.assertEqual(status, 1)
        self.assertIn("name.grammar", stderr)
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target), "--only", "../x")
        self.assertEqual(status, 1)
        self.assertIn("name.grammar", stderr)

    def test_link_set_does_not_write_its_record_through_a_link(self):
        target = self.root.parent / "home" / ".claude" / "skills"
        target.mkdir(parents=True)
        outside = self.root.parent / "home" / "elsewhere.txt"
        outside.write_text("mine\n", encoding="utf-8")
        record = target / ".ai-tools-assets-acme.links"
        record.symlink_to(outside)
        status, _, stderr = run("link-set", "acme", "--root", str(self.root), "--target", str(target))
        self.assertEqual(status, 1)
        self.assertIn("the record is not a regular file", stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "mine\n", "the link's target is not written")
        self.assertFalse((target / "acme-pdf-processing").exists(), "nothing is placed on a record that is refused")


# The template's skeleton files carry SPDX headers and annotations as string literals; `reuse lint` reads each as an
# expression of this file, so the constants and the class sit in an ignored block.
# REUSE-IgnoreStart
TEMPLATE_PUBLISHER_CONF = """# Who publishes this repository's sets.
publisher=change-me
contact=change-me@example.com
maintainers=[change-me@example.com]
source=https://github.com/change-me/ai-tools-assets
description="Skills and subagents for coding agents, released as versioned sets"
"""
TEMPLATE_REUSE = """version = 1
SPDX-PackageName = "ai-tools-assets-template"
SPDX-PackageDownloadLocation = "https://github.com/dag-node/ai-tools-assets-template"

[[annotations]]
path = "**"
precedence = "closest"
SPDX-FileCopyrightText = "2026 the ai-tools-assets-template authors"
SPDX-License-Identifier = "MIT-0"
"""
TEMPLATE_CODEOWNERS = """* @dag-node/ai-tools-maintainers
/publisher.conf @dag-node/ai-tools-maintainers
/keys/ @dag-node/ai-tools-maintainers
/.claude-plugin/ @dag-node/ai-tools-maintainers
/.github/ @dag-node/ai-tools-maintainers
"""
TEMPLATE_VALIDATE = """# SPDX-License-Identifier: MIT-0
name: validate
on:
  pull_request:
jobs:
  ai-tools-assets-template:
    uses: dag-node/ai-tools-assets-tools/.github/workflows/validate.yml@b11c27282193da07f1067ce905de6974e420c433 # v1.5.0
    with:
      enforce-signoff: true
"""
TEMPLATE_RELEASE = """# SPDX-License-Identifier: MIT-0
name: release
on:
  push:
    tags: ['*/v*']
jobs:
  ai-tools-assets-template:
    uses: dag-node/ai-tools-assets-tools/.github/workflows/set-release.yml@b11c27282193da07f1067ce905de6974e420c433 # v1.5.0
    secrets: inherit
    with:
      tag-signer-fingerprints: change-me
      tag-signer-key-url: https://github.com/change-me.gpg
      artifact-signer-fingerprint: change-me
"""
INITIALIZE = ("--repository", "Acme/ai-tools-assets", "--contact", "tools@acme.example")


def tree_state(root: pathlib.Path):
    """Every path under `root` with its bytes, or its link target, so a refusal is shown to leave the tree whole."""
    state = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[relative] = ("link", os.readlink(path))
        elif path.is_file():
            state[relative] = ("file", path.read_bytes())
        else:
            state[relative] = ("directory",)
    return state


def compose_template(root: pathlib.Path) -> None:
    """Write the template's tree at `root` as it is made: the placeholder publisher.conf, `new-set` and `new-asset` for
    the example set, and the skeleton files with the lines new-publisher rewrites."""
    (root / "sets").mkdir(parents=True)
    (root / "LICENSES").mkdir()
    shutil.copy(FIXTURES / "LICENSES" / "MIT-0.txt", root / "LICENSES" / "MIT-0.txt")
    (root / "publisher.conf").write_text(TEMPLATE_PUBLISHER_CONF, encoding="utf-8")
    for command in (("new-set", "change-me-example", "--license", "MIT-0", "--summary", "An example set; rename it to start with your publisher name"),
                    ("new-asset", "change-me-example", "skill", "change-me-example-hello", "--description", "Prints a greeting. Replace it with your first skill.")):
        status, _, stderr = run(*command, "--root", str(root))
        assert status == 0, stderr
    mit0 = (FIXTURES / "LICENSES" / "MIT-0.txt").read_text(encoding="utf-8")
    files = {"LICENSE": mit0.replace("<YEAR> <COPYRIGHT HOLDER>", "2026 the ai-tools-assets-template authors"),
             "REUSE.toml": TEMPLATE_REUSE, ".github/CODEOWNERS": TEMPLATE_CODEOWNERS, "README.md": "# ai-tools-assets-template\n",
             ".github/workflows/validate.yml": TEMPLATE_VALIDATE, ".github/workflows/release.yml": TEMPLATE_RELEASE}
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")


class NewPublisher(unittest.TestCase):
    """new-publisher over a tree composed the way the template is made."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.root = self.scratch / "ai-tools-assets"
        compose_template(self.root)

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def initialize(self, *arguments):
        return run("new-publisher", "--root", str(self.root), *(arguments or INITIALIZE))

    def test_initializes_a_copy_that_passes_every_check(self):
        status, stdout, stderr = self.initialize()
        self.assertEqual((status, stderr), (0, ""))
        for command in (("validate",), ("sync-manifests", "--check"), ("check-licenses",), ("check-publisher", "--repository", "Acme/ai-tools-assets")):
            status, _, stderr = run(*command, "--root", str(self.root))
            self.assertEqual((status, stderr), (0, ""), command)
        self.assertEqual(sorted(path.name for path in (self.root / "sets").iterdir()), ["acme"])
        self.assertTrue((self.root / "sets" / "acme" / "skills" / "acme-hello" / "SKILL.md").is_file())
        year = datetime.datetime.now(datetime.timezone.utc).year
        self.assertIn("  ai-tools-assets:\n", (self.root / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8"))
        self.assertIn("* @Acme\n", (self.root / ".github" / "CODEOWNERS").read_text(encoding="utf-8"))
        reuse = (self.root / "REUSE.toml").read_text(encoding="utf-8")
        self.assertIn('SPDX-PackageName = "ai-tools-assets"', reuse)
        self.assertIn('SPDX-PackageDownloadLocation = "https://github.com/Acme/ai-tools-assets"', reuse)
        self.assertIn(f'SPDX-FileCopyrightText = "{year} Acme"', reuse)
        self.assertIn(f"Copyright (c) {year} Acme\n", (self.root / "LICENSE").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "README.md").read_text(encoding="utf-8").startswith("# ai-tools-assets\n"))
        for path in self.root.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8").lower()
                self.assertNotIn("ai-tools-assets-template", text, path)
                if path.name != "release.yml":
                    self.assertNotIn("change-me", text, path)
        self.assertIn('new-publisher: next, commit the initialization: git add -A && git commit -s -m "chore: initialize the publisher acme"', stdout)

    def test_a_source_outside_github_passes_a_pull_request_and_not_the_release_check(self):
        status, _, stderr = self.initialize(*INITIALIZE, "--source", "https://git.acme.example/a b")
        self.assertEqual(status, 1)
        self.assertIn("repo.publisher-conf: the source `https://git.acme.example/a b` is not a URL", stderr)
        status, _, stderr = self.initialize(*INITIALIZE, "--source", "https://git.acme.example/assets")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("source=https://git.acme.example/assets\n", (self.root / "publisher.conf").read_text(encoding="utf-8"))
        self.assertIn('SPDX-PackageDownloadLocation = "https://git.acme.example/assets"', (self.root / "REUSE.toml").read_text(encoding="utf-8"))
        for command in (("validate",), ("check-publisher",)):
            status, _, stderr = run(*command, "--root", str(self.root))
            self.assertEqual((status, stderr), (0, ""), command)
        status, _, stderr = run("check-publisher", "--root", str(self.root), "--repository", "Acme/ai-tools-assets")
        self.assertIn("source is `https://git.acme.example/assets`", stderr, "a GitHub release keeps requiring the GitHub form")

    def test_a_second_run_is_refused_and_leaves_the_tree_whole(self):
        self.assertEqual(self.initialize()[0], 0)
        before = tree_state(self.root)
        status, _, stderr = self.initialize()
        self.assertEqual(status, 1)
        self.assertIn("publisher.conf: repo.publisher-conf: names the publisher `acme`: the copy is initialized already", stderr)
        self.assertEqual(tree_state(self.root), before)

    def test_each_refusal_leaves_the_tree_whole(self):
        before = tree_state(self.root)
        cases = [
            (("--repository", "Acme/ai-tools-assets", "--contact", "change-me@acme.example"), "repo.publisher-conf"),
            (("--repository", "Acme/ai-tools-assets", "--contact", "tools@acme.example", "--maintainers", "someone@dagnode.com"), "repo.publisher-conf"),
            (("--repository", "OpenAI/ai-tools-assets", "--contact", "tools@acme.example"), "name.reserved-word"),
            (("--repository", "acme/3d-assets", "--contact", "tools@acme.example"), "repo.layout"),
            (("--repository", "Acme/ai-tools-assets", "--contact", "tools@acme.example", "--tag-signer", "0" * 39), "repo.layout"),
            (("--repository", "Acme/ai-tools-assets", "--contact", "tools@acme.example", "--license", "Apache-2.0"), "license.text"),
            (("--repository", "Acme/ai-tools-assets", "--contact", "tools@acme.example", "--license", "GPL-3.0-only"), "license.allowlist"),
            (("--repository", "Acme/ai-tools-assets", "--contact", "tools@acme.example\nx_key=1"), "repo.publisher-conf"),
            (("--repository", "Acme/ai-tools-assets", "--contact", "tools@acme.example", "--code-owner", "tools@acme.example"), "repo.layout"),
        ]
        for arguments, rule in cases:
            status, _, stderr = self.initialize(*arguments)
            self.assertEqual(status, 1, arguments)
            self.assertIn(f": {rule}: ", stderr, arguments)
            self.assertEqual(tree_state(self.root), before, arguments)
        status, _, stderr = self.initialize("--repository", "acme/3d-assets", "--contact", "tools@acme.example")
        self.assertIn("name one with --job-id", stderr)
        (self.root / "sets" / "acme").mkdir()
        status, _, stderr = self.initialize()
        self.assertEqual(status, 1)
        self.assertIn("sets: repo.layout: holds `acme`, `change-me-example`", stderr)
        (self.root / "sets" / "acme").rmdir()
        workflow = self.root / ".github" / "workflows" / "validate.yml"
        workflow.write_text(TEMPLATE_VALIDATE.replace("  ai-tools-assets-template:", "  mine:"), encoding="utf-8")
        before = tree_state(self.root)
        status, _, stderr = self.initialize()
        self.assertEqual(status, 1)
        self.assertIn(".github/workflows/validate.yml: repo.layout: lacks the template line `ai-tools-assets-template:`", stderr)
        self.assertEqual(tree_state(self.root), before)

    def test_the_placeholder_is_refused_as_a_whole_token(self):
        before = tree_state(self.root)
        for arguments in (("--holder", "change-me-example"), ("--holder", "Change-Me"), ("--contact", "change-me@example.com"),
                          ("--tag-signer", "A" * 40, "--tag-signer-key-url", "https://github.com/change-me.gpg"),
                          ("--source", "https://example.org/change-me/ai-tools-assets")):
            status, _, stderr = self.initialize(*INITIALIZE, *arguments)
            self.assertEqual(status, 1, arguments)
            self.assertIn("holds the template placeholder `change-me`", stderr, arguments)
            self.assertEqual(tree_state(self.root), before, arguments)
        status, _, stderr = self.initialize("--repository", "exchange-media/ai-tools-assets", "--contact", "richange-meter@acme.example")
        self.assertEqual((status, stderr), (0, ""), "a name that carries the letters inside a word is the publisher's own")
        status, _, stderr = run("check-publisher", "--root", str(self.root), "--repository", "exchange-media/ai-tools-assets")
        self.assertEqual((status, stderr), (0, ""))

    def test_does_not_write_through_a_link(self):
        outside = self.scratch / "outside"
        outside.mkdir()
        (outside / "LICENSE").write_text("theirs\n", encoding="utf-8")
        (self.root / "LICENSE").unlink()
        (self.root / "LICENSE").symlink_to(outside / "LICENSE")
        status, _, stderr = self.initialize()
        self.assertEqual(status, 1)
        self.assertIn("LICENSE: file.symlink: ", stderr)
        self.assertEqual((outside / "LICENSE").read_text(encoding="utf-8"), "theirs\n")
        (self.root / "LICENSE").unlink()
        (self.root / "LICENSE").write_text("Copyright 2026 the ai-tools-assets-template authors\n", encoding="utf-8")
        shutil.move(str(self.root / "sets" / "change-me-example"), str(outside / "change-me-example"))
        (self.root / "sets" / "change-me-example").symlink_to(outside / "change-me-example")
        status, _, stderr = self.initialize()
        self.assertEqual(status, 1)
        self.assertIn("sets/change-me-example: file.symlink: ", stderr)
        self.assertTrue((outside / "change-me-example" / "set.conf").is_file(), "the link's target is not removed")
        (self.root / "sets" / "change-me-example").unlink()
        (self.root / "sets").rename(outside / "sets")
        shutil.move(str(outside / "change-me-example"), str(outside / "sets" / "change-me-example"))
        (self.root / "sets").symlink_to(outside / "sets")
        before = tree_state(self.root)
        status, _, stderr = self.initialize()
        self.assertEqual(status, 1)
        self.assertIn("sets: file.symlink: ", stderr)
        self.assertEqual(tree_state(self.root), before, "a link at sets/ is refused before the first write")
        self.assertTrue((outside / "sets" / "change-me-example" / "set.conf").is_file())

    def test_removes_a_link_inside_the_example_set_and_not_its_target(self):
        outside = self.scratch / "outside"
        (outside / "kept").mkdir(parents=True)
        (outside / "kept" / "file.txt").write_text("theirs\n", encoding="utf-8")
        example = self.root / "sets" / "change-me-example"
        (example / "skills" / "linked").symlink_to(outside / "kept")
        (example / "linked-file").symlink_to(outside / "kept" / "file.txt")
        status, _, stderr = self.initialize()
        self.assertEqual((status, stderr), (0, ""))
        self.assertFalse(example.exists() or example.is_symlink())
        self.assertEqual((outside / "kept" / "file.txt").read_text(encoding="utf-8"), "theirs\n")

    def test_the_signer_inputs(self):
        status, stdout, _ = self.initialize(*INITIALIZE, "--tag-signer", "A" * 40, "--artifact-signer", "B" * 40)
        self.assertEqual(status, 0)
        release = (self.root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn(f"      tag-signer-fingerprints: {'A' * 40}\n", release)
        self.assertIn("      tag-signer-key-url: https://github.com/Acme.gpg\n", release)
        self.assertIn(f"      artifact-signer-fingerprint: {'B' * 40}\n", release)
        self.assertNotIn("set the release signers", stdout)
        self.tearDown()
        self.setUp()
        status, stdout, _ = self.initialize()
        release = (self.root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertEqual(release.count("change-me"), 3)
        self.assertIn("next, set the release signers in .github/workflows/release.yml", stdout)

    def test_the_licence(self):
        status, _, stderr = self.initialize()
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual((self.root / "LICENSES" / "MIT.txt").read_bytes(), (REPOSITORY / "LICENSES" / "MIT.txt").read_bytes(),
                         "the text as published, from the tools")
        self.assertFalse((self.root / "LICENSES" / "MIT-0.txt").exists(), "an unused text fails reuse lint")
        for workflow in ("validate.yml", "release.yml"):
            self.assertTrue((self.root / ".github" / "workflows" / workflow).read_text(encoding="utf-8").startswith("# SPDX-License-Identifier: MIT\n"))
        status, stdout, _ = run("check-licenses", "--root", str(self.root))
        self.assertIn("every file is under MIT (permissive)", stdout)
        self.tearDown()
        self.setUp()
        status, _, stderr = self.initialize(*INITIALIZE, "--license", "MIT-0")
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(sorted(path.name for path in (self.root / "LICENSES").iterdir()), ["MIT-0.txt"])
        self.tearDown()
        self.setUp()
        status, _, stderr = self.initialize(*INITIALIZE, "--license", "Apache-2.0")
        self.assertEqual(status, 1)
        self.assertIn("license.text: no `Apache-2.0.txt` in the copy's LICENSES/ or the tools'; place it with `reuse download Apache-2.0`", stderr)
        (self.root / "LICENSES" / "Apache-2.0.txt").write_text("Apache License\nVersion 2.0, January 2004\n", encoding="utf-8")
        status, _, stderr = self.initialize(*INITIALIZE, "--license", "Apache-2.0")
        self.assertEqual(status, 0, stderr)
        self.assertEqual((self.root / "LICENSE").read_text(encoding="utf-8"), "Apache License\nVersion 2.0, January 2004\n",
                         "a text without a holder line is written as published")

    def test_the_holder_line_of_every_default_list_licence_that_carries_one_is_filled(self):
        year = datetime.datetime.now(datetime.timezone.utc).year
        # The opening lines of each text as SPDX publishes it, trailing blanks included, and the line LICENSE holds.
        cases = {
            "MIT": ("MIT License\n\nCopyright (c) <year> <copyright holders>\n", f"Copyright (c) {year} Acme"),
            "MIT-0": ("MIT No Attribution\n\nCopyright <YEAR> <COPYRIGHT HOLDER>\n", f"Copyright {year} Acme"),
            "ISC": ("ISC License\n\nCopyright <year> <owner> \n", f"Copyright {year} Acme"),
            "BSD-2-Clause": ("Copyright (c) <year> <owner> \n", f"Copyright (c) {year} Acme"),
            "BSD-3-Clause": ("Copyright (c) <year> <owner>. \n", f"Copyright (c) {year} Acme."),
            "0BSD": ("Copyright (C) YEAR by AUTHOR EMAIL\n", f"Copyright (C) {year} Acme"),
        }
        for identifier, (opening, holder_line) in cases.items():
            self.tearDown()
            self.setUp()
            (self.root / "LICENSES" / f"{identifier}.txt").write_text(opening + "\nPermission is granted.\n", encoding="utf-8")
            status, _, stderr = self.initialize(*INITIALIZE, "--license", identifier)
            self.assertEqual(status, 0, (identifier, stderr))
            license_text = (self.root / "LICENSE").read_text(encoding="utf-8")
            self.assertIn(holder_line, license_text.splitlines(), identifier)
            self.assertNotIn("year>", license_text.lower(), identifier)
            self.assertNotIn("YEAR", license_text, identifier)


class CheckPublisherRelease(unittest.TestCase):
    """`check-publisher --repository` over the template as generated and as initialized."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.root = self.scratch / "ai-tools-assets"
        compose_template(self.root)

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def check(self, *arguments):
        status, _, stderr = run("check-publisher", "--root", str(self.root), *arguments)
        return status, stderr

    def test_a_copy_as_generated_passes_a_pull_request_and_fails_its_release(self):
        self.assertEqual(self.check(), (0, ""))
        status, stderr = self.check("--repository", "change-me/ai-tools-assets")
        self.assertEqual(status, 1, "an owner named change-me passed on the owner comparison alone")
        for key in ("publisher", "contact", "maintainers", "source"):
            self.assertIn(f"publisher.conf: `{key}` still holds the template placeholder `change-me`", stderr, key)
        self.assertIn("REUSE.toml: `SPDX-PackageName` still names the template", stderr)

    def test_each_identity_key_holding_the_placeholder_is_refused_at_release(self):
        status, _, stderr = run("new-publisher", "--root", str(self.root), *INITIALIZE)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.check("--repository", "Acme/ai-tools-assets"), (0, ""))
        conf = self.root / "publisher.conf"
        original = conf.read_text(encoding="utf-8")
        for key, old, new in (("contact", "contact=tools@acme.example", "contact=Change-Me@acme.example"),
                              ("maintainers", "maintainers=[tools@acme.example]", "maintainers=[tools@acme.example, change-me@acme.example]"),
                              ("source", "source=https://github.com/Acme/ai-tools-assets", "source=https://github.com/Acme/change-me")):
            conf.write_text(original.replace(old, new), encoding="utf-8")
            status, stderr = self.check("--repository", "Acme/ai-tools-assets")
            self.assertEqual(status, 1, key)
            self.assertIn(f"publisher.conf: `{key}` still holds the template placeholder `change-me`", stderr, key)
        for value in ("change-me-example", "change-me@example.com", "https://github.com/change-me.gpg", "change-me/ai-tools-assets", "Change-Me"):
            conf.write_text(original.replace("contact=tools@acme.example", f"contact={value}"), encoding="utf-8")
            status, stderr = self.check("--repository", "Acme/ai-tools-assets")
            self.assertEqual(status, 1, value)
            self.assertIn("publisher.conf: `contact` still holds the template placeholder `change-me`", stderr, value)
        conf.write_text(original.replace("contact=tools@acme.example", "contact=richange-meter@acme.example"), encoding="utf-8")
        status, stderr = self.check("--repository", "Acme/ai-tools-assets")
        self.assertIn("plugin.json", stderr, "control: the manifests no longer carry the contact")
        self.assertNotIn("placeholder", stderr, "the letters inside a word are not the token")
        conf.write_text(original, encoding="utf-8")

    def test_the_template_package_name_is_refused_at_release_alone(self):
        status, _, stderr = run("new-publisher", "--root", str(self.root), *INITIALIZE)
        self.assertEqual(status, 0, stderr)
        reuse = self.root / "REUSE.toml"
        reuse.write_text(reuse.read_text(encoding="utf-8").replace('SPDX-PackageName = "ai-tools-assets"', 'SPDX-PackageName = "ai-tools-assets-template"'),
                         encoding="utf-8")
        self.assertEqual(self.check(), (0, ""), "a pull request does not read the package name")
        status, stderr = self.check("--repository", "Acme/ai-tools-assets")
        self.assertEqual(status, 1)
        self.assertIn("REUSE.toml: `SPDX-PackageName` still names the template; run new-publisher", stderr)
# REUSE-IgnoreEnd


class ScratchDirectory(unittest.TestCase):
    """openpgp.scratch_directory, which sign-set and verify-set write their scratch material into."""

    def test_the_handlers_are_installed_for_the_context_and_restored_after(self):
        import openpgp
        before = {number: signal.getsignal(number) for number in openpgp.TERMINATION_SIGNALS}
        with self.assertRaises(SystemExit) as raised:
            with openpgp.scratch_directory("probe.") as scratch:
                self.assertTrue(scratch.is_dir())
                for number in openpgp.TERMINATION_SIGNALS:
                    self.assertNotEqual(signal.getsignal(number), before[number], number)
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertFalse(scratch.exists())
        self.assertEqual({number: signal.getsignal(number) for number in openpgp.TERMINATION_SIGNALS}, before)

    def test_a_process_sent_a_termination_signal_removes_its_scratch_directory(self):
        code = "import openpgp, time\nwith openpgp.scratch_directory('probe.') as scratch:\n    print(scratch, flush=True)\n    time.sleep(60)\n"
        for number in (signal.SIGTERM, signal.SIGHUP):
            child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                                     env=dict(os.environ, PYTHONPATH=str(TOOLS / "lib")))
            scratch = pathlib.Path(child.stdout.readline().strip())
            self.assertTrue(scratch.is_dir(), "control: the directory exists while the context runs")
            child.send_signal(number)
            self.assertEqual(child.wait(timeout=10), 128 + number)
            child.stdout.close()
            self.assertFalse(scratch.exists(), number)


class SignatureStatus(unittest.TestCase):
    """openpgp.check_signature over status streams gpgv may print, the process mocked, so the policy is held without
    gpg."""

    SIGNER = "A" * 40
    PRIMARY = "B" * 40
    OTHER = "C" * 40

    def validsig(self, fingerprint=SIGNER, primary=PRIMARY):
        return f"[GNUPG:] VALIDSIG {fingerprint} 2026-01-01 1767225600 0 4 0 22 8 00{' ' + primary if primary else ''}\n"

    def good(self, fingerprint=SIGNER, primary=PRIMARY):
        return f"[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG {fingerprint[-16:]} Acme\n" + self.validsig(fingerprint, primary)

    def expired(self, fingerprint=OTHER):
        return f"[GNUPG:] NEWSIG\n[GNUPG:] EXPSIG {fingerprint[-16:]} Acme\n" + self.validsig(fingerprint, fingerprint)

    def check(self, stdout, returncode=0):
        import openpgp
        completed = subprocess.CompletedProcess([], returncode, stdout, "gpgv: the last line\n")
        with tempfile.TemporaryDirectory() as scratch, \
                mock.patch.object(openpgp.shutil, "which", return_value="/usr/bin/gpgv"), \
                mock.patch.object(openpgp.subprocess, "run", return_value=completed):
            result = openpgp.check_signature(pathlib.Path(scratch), b"key", b"data", b"signature")
        return result.outcome, result.primary

    def test_a_complete_good_signature_alone_verifies(self):
        import openpgp
        self.assertEqual(self.check(self.good()), (openpgp.VERIFIED, self.PRIMARY), "control: ten fields, the tenth the primary")
        self.assertEqual(self.check(self.good(primary=None)), (openpgp.VERIFIED, self.SIGNER), "nine fields: the signing key is the primary")
        self.assertEqual(self.check(self.good() + "[GNUPG:] KEYEXPIRED 1700000000\n"), (openpgp.VERIFIED, self.PRIMARY),
                         "an expired subkey the signature does not use")

    def test_a_second_signature_leaves_the_check_unable_in_either_order(self):
        import openpgp
        errsig = f"[GNUPG:] NEWSIG\n[GNUPG:] ERRSIG {self.OTHER[-16:]} 22 8 00 1767225600 4\n"
        for stdout, returncode in ((self.good() + self.expired(), 0), (self.expired() + self.good(), 0), (self.good() + errsig, 2),
                                   (self.good() + self.good(self.OTHER, self.OTHER), 0)):
            self.assertEqual(self.check(stdout, returncode), (openpgp.UNABLE, None), stdout)
        without_newsig = self.good().replace("[GNUPG:] NEWSIG\n", "")
        self.assertEqual(self.check(without_newsig + without_newsig.replace(self.SIGNER, self.OTHER)), (openpgp.UNABLE, None),
                         "two result lines count as two signatures without a NEWSIG")

    def test_an_expired_signature_and_an_expired_or_revoked_key_are_mismatches(self):
        import openpgp
        self.assertEqual(self.check(self.expired()), (openpgp.MISMATCH, None))
        for keyword in ("EXPKEYSIG", "REVKEYSIG"):
            self.assertEqual(self.check(self.good().replace("GOODSIG", keyword)), (openpgp.MISMATCH, None), keyword)
        self.assertEqual(self.check(self.good() + "[GNUPG:] KEYREVOKED\n"), (openpgp.MISMATCH, None))
        self.assertEqual(self.check(self.good().replace("GOODSIG", "BADSIG")[:-len(self.validsig())], 1), (openpgp.MISMATCH, None))

    def test_an_incomplete_record_or_a_failed_exit_leaves_the_check_unable(self):
        import openpgp
        one_field = f"[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG {self.SIGNER[-16:]} Acme\n[GNUPG:] VALIDSIG {self.SIGNER}\n"
        eight_fields = self.good(primary=None).replace(" 00\n", "\n")
        for stdout, returncode in ((one_field, 0), (eight_fields, 0), (self.good(), 2), (self.good().replace("GOODSIG", "ERRSIG"), 0),
                                   (self.validsig(), 0), ("", 0)):
            self.assertEqual(self.check(stdout, returncode), (openpgp.UNABLE, None), (stdout, returncode))
        self.assertEqual(self.check(self.good()), (openpgp.VERIFIED, self.PRIMARY), "control: the same stream, complete, exit 0")

    def test_verify_set_exits_2_for_a_file_of_two_signatures(self):
        verify_set = load_command("verify-set")
        import openpgp
        completed = subprocess.CompletedProcess([], 0, self.good() + self.expired(), "")
        with tempfile.TemporaryDirectory() as directory:
            for name in ("SHA256SUMS", "SHA256SUMS.asc"):
                (pathlib.Path(directory) / name).write_text("x\n", encoding="utf-8")
            run_record = verify_set.Verification(pathlib.Path(directory))
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(openpgp.shutil, "which", return_value="/usr/bin/gpgv"), \
                        mock.patch.object(openpgp.subprocess, "run", return_value=completed):
                    verify_set.verify(run_record, mock.Mock(source=None), None, b"key", directory_fd)
            finally:
                os.close(directory_fd)
        self.assertEqual(run_record.status, openpgp.UNABLE)
        self.assertEqual([(finding.path, finding.message) for finding in run_record.collector.findings],
                         [("SHA256SUMS.asc", "the signature file holds more than one signature; a set is signed once")])


class SignSet(unittest.TestCase):
    """sign-set's refusals that come before gpg runs."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "acme-assets"
        compose_publisher(self.root)

    def tearDown(self):
        shutil.rmtree(self.root.parent)

    def test_a_set_build_set_did_not_stage_is_refused(self):
        status, _, stderr = run("sign-set", "acme", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("repo.layout: is not a set build-set staged", stderr)
        self.assertIn("run build-set acme first", stderr)

    def test_a_stale_stage_is_refused_before_gpg_runs(self):
        status, _, stderr = run("build-set", "acme", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        readme = self.root / "build" / "acme" / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "Edited after the build.\n", encoding="utf-8")
        status, _, stderr = run("sign-set", "acme", "--root", str(self.root), env=dict(os.environ, PATH="/nonexistent"))
        self.assertEqual(status, 1)
        self.assertIn("release.inventory", stderr)
        self.assertIn("nothing was signed", stderr)
        self.assertNotIn("is not on PATH", stderr, "the stage is judged before gpg is looked for")
        self.assertFalse((self.root / "build" / "acme" / "SHA256SUMS.asc").exists())

    def test_the_bytes_signed_are_the_inventory_validation_read(self):
        status, _, stderr = run("build-set", "acme", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        from findings import FindingCollector
        collector = FindingCollector("sign-set")
        build_root = self.root / "build"
        build_fd = os.open(build_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            inventory = load_command("sign-set").validate_stage(self.root, build_root, build_fd, build_root / "acme", collector)
        finally:
            os.close(build_fd)
        self.assertEqual(collector.findings, [])
        self.assertEqual(inventory, (build_root / "acme" / "SHA256SUMS").read_bytes())


class SignedSetCase(unittest.TestCase):
    """A staged acme set and a key generated for the test, in a GnuPG home of its own."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.root = self.scratch / "acme-assets"
        compose_publisher(self.root)
        self.gnupg = self.scratch / "gnupg"
        self.gnupg.mkdir(mode=0o700)
        self.environment = dict(os.environ, GNUPGHOME=str(self.gnupg))
        self.gpg("--quick-generate-key", "--passphrase", "", "Acme <tools@acme.example>", "ed25519", "sign", "never")
        self.primary = next(line.split(":")[9] for line in self.gpg("--with-colons", "--list-keys").splitlines()
                            if line.startswith("fpr:"))
        status, _, stderr = run("build-set", "acme", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        self.signature = self.root / "build" / "acme" / "SHA256SUMS.asc"

    def tearDown(self):
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=self.environment, capture_output=True)
        shutil.rmtree(self.scratch)

    def gpg(self, *arguments: str) -> str:
        return subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", *arguments], capture_output=True, text=True,
                              check=True, env=self.environment).stdout

    def sign(self, *arguments: str):
        return run("sign-set", "acme", "--root", str(self.root), *arguments, env=self.environment)


@unittest.skipUnless(gpg_runs(), "signing needs gpg and gpgv")
class SignSetWithAKey(SignedSetCase):
    """sign-set with the generated key."""

    def test_signs_and_prints_the_primary_a_binding_names(self):
        status, stdout, stderr = self.sign()
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"signers=openpgp:{self.primary}", stdout)
        import openpgp
        key = subprocess.run(["gpg", "--export", self.primary], capture_output=True, check=True, env=self.environment).stdout
        with tempfile.TemporaryDirectory() as scratch:
            check = openpgp.check_signature(pathlib.Path(scratch), key, (self.root / "build" / "acme" / "SHA256SUMS").read_bytes(),
                                            self.signature.read_bytes())
        self.assertEqual((check.outcome, check.primary), (openpgp.VERIFIED, self.primary))

    def test_a_key_the_keyring_lacks_is_refused_and_nothing_is_written(self):
        status, _, stderr = self.sign("--key", "0" * 40)
        self.assertEqual(status, 1)
        self.assertIn("release.signature", stderr)
        self.assertFalse(self.signature.exists())

    def test_a_second_run_replaces_the_signature_and_a_link_at_its_name_is_refused(self):
        self.assertEqual(self.sign("--key", self.primary)[0], 0)
        first = self.signature.read_bytes()
        self.assertEqual(self.sign()[0], 0)
        self.assertTrue(self.signature.is_file() and not self.signature.is_symlink())
        self.assertTrue(self.signature.read_bytes().startswith(b"-----BEGIN PGP SIGNATURE-----"), first)
        outside = self.scratch / "outside.asc"
        outside.write_text("theirs\n", encoding="utf-8")
        self.signature.unlink()
        self.signature.symlink_to(outside)
        status, _, stderr = self.sign()
        self.assertEqual(status, 1)
        self.assertIn("file.symlink", stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "theirs\n")


class VerifySet(unittest.TestCase):
    """verify-set's inventory half over the conformance fixtures, and the refusals that come before gpgv runs."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.root = self.scratch / "acme-assets"
        compose_publisher(self.root)
        status, _, stderr = run("build-set", "acme", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        self.stage = self.root / "build" / "acme"
        self.key = self.scratch / "key.asc"
        self.key.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmDMEZQ==\n=abcd\n-----END PGP PUBLIC KEY BLOCK-----\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def inventory(self, set_directory: pathlib.Path):
        import signed_inventory
        directory_fd = os.open(set_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return signed_inventory.verify_inventory(directory_fd, (set_directory / "SHA256SUMS").read_text(encoding="utf-8"))
        finally:
            os.close(directory_fd)

    def test_the_inventory_half_holds_the_release_fixtures_and_a_fresh_build(self):
        import signed_inventory
        check = self.inventory(self.stage)
        self.assertEqual((check.outcome, check.problems), (signed_inventory.VERIFIED, []), "control: the stage build-set wrote")
        for name in ("release.inventory", "release.inventory.duplicate"):
            check = self.inventory(FIXTURES / "fail" / name / "acme")
            self.assertEqual(check.outcome, signed_inventory.MISMATCH, name)
        (self.stage / "attestations").mkdir()
        (self.stage / "attestations" / "timestamp.tsr").write_text("a statement\n", encoding="utf-8")
        check = self.inventory(self.stage)
        self.assertEqual(check.outcome, signed_inventory.VERIFIED, "attestations/ lies outside the inventory")

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_an_unreadable_file_leaves_the_inventory_unable_naming_it(self):
        import signed_inventory
        readme = self.stage / "README.md"
        readme.chmod(0)
        try:
            check = self.inventory(self.stage)
        finally:
            readme.chmod(0o644)
        self.assertEqual((check.outcome, check.problems), (signed_inventory.UNABLE, [("README.md", "cannot be read: Permission denied")]))
        self.assertEqual(self.inventory(self.stage).outcome, signed_inventory.VERIFIED, "control: the same file readable")

    def test_an_entry_the_reader_refuses_leaves_the_inventory_unable_naming_it(self):
        import signed_inventory
        readme = self.stage / "README.md"
        outside = self.scratch / "outside"
        outside.mkdir()
        os.link(readme, outside / "README.md")
        check = self.inventory(self.stage)
        self.assertEqual((check.outcome, check.problems[0][0]), (signed_inventory.UNABLE, "README.md"), check.problems)
        self.assertIn("has 2 links", check.problems[0][1])
        (outside / "README.md").unlink()
        readme.rename(outside / "README.md")
        readme.symlink_to(outside / "README.md")
        check = self.inventory(self.stage)
        self.assertEqual((check.outcome, check.problems), (signed_inventory.UNABLE, [("README.md", check.problems[0][1])]))
        self.assertIn("is a symbolic link", check.problems[0][1])
        readme.unlink()
        (outside / "README.md").rename(readme)
        self.assertEqual(self.inventory(self.stage).outcome, signed_inventory.VERIFIED, "control: the file restored")
        (self.stage / "agents").rename(outside / "agents")
        (self.stage / "agents").symlink_to(outside / "agents")
        check = self.inventory(self.stage)
        self.assertEqual((check.outcome, check.problems), (signed_inventory.UNABLE, [("agents", check.problems[0][1])]),
                         "a link at a directory name, and no listed-and-absent finding for the files under it")
        self.assertIn("is a symbolic link", check.problems[0][1])

    def test_an_input_it_cannot_read_exits_2(self):
        status, stdout, stderr = run("verify-set", str(self.stage), "--key", str(self.key))
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("SHA256SUMS.asc: release.signature: is absent", stderr)
        (self.stage / "SHA256SUMS.asc").write_text("not a signature\n", encoding="utf-8")
        for key_text in ("not a key\n", "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n!!!\n-----END PGP PUBLIC KEY BLOCK-----\n"):
            self.key.write_text(key_text, encoding="utf-8")
            status, _, stderr = run("verify-set", str(self.stage), "--key", str(self.key))
            self.assertEqual(status, 2, key_text)
            self.assertIn("key.asc: release.signature: is ", stderr)
        status, _, stderr = run("verify-set", str(self.stage), "--key", str(self.key), "--signer", "ABC")
        self.assertEqual(status, 2)
        self.assertIn("`ABC` is not a fingerprint of 40 hex digits", stderr)

    def test_the_key_is_read_through_the_reader(self):
        armored = self.scratch / "armored.asc"
        armored.write_text(self.key.read_text(encoding="utf-8"), encoding="utf-8")
        self.key.unlink()
        self.key.symlink_to(armored)
        status, _, stderr = run("verify-set", str(self.stage), "--key", str(self.key))
        self.assertEqual(status, 2)
        self.assertIn("key.asc: release.signature: is a symbolic link", stderr)
        status, _, stderr = run("verify-set", str(self.stage), "--key", str(armored))
        self.assertEqual(status, 2)
        self.assertIn("SHA256SUMS.asc: release.signature: is absent", stderr, "control: the link's target is read and the run goes on")
        self.key.unlink()
        self.key.write_bytes(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n" + b"A" * 64 * 1024)
        status, _, stderr = run("verify-set", str(self.stage), "--key", str(self.key))
        self.assertEqual(status, 2)
        self.assertIn("key.asc: release.signature: is 65574 bytes, over the 65536 a public key takes", stderr)

    def test_the_source_comparison_reads_set_conf_under_the_set_and_holds_it_to_the_inventory(self):
        verify_set = load_command("verify-set")
        import signed_inventory

        def compare(listed):
            verification = verify_set.Verification(self.stage)
            directory_fd = os.open(self.stage, os.O_RDONLY | os.O_DIRECTORY)
            try:
                verify_set.compare_with_source(verification, directory_fd, self.root, listed)
            finally:
                os.close(directory_fd)
            return verification.status, [(finding.path, finding.message) for finding in verification.collector.findings]

        listed = signed_inventory.parse_inventory((self.stage / "SHA256SUMS").read_text(encoding="utf-8"), signed_inventory.InventoryCheck())
        self.assertEqual(compare(listed), (0, []), "control: the source the stage was built from")
        self.assertEqual(compare(dict(listed, **{"set.conf": "0" * 64})), (2, [("set.conf", "changed since the inventory was verified")]))
        set_conf = self.stage / "set.conf"
        set_conf.rename(self.scratch / "set.conf")
        set_conf.symlink_to(self.scratch / "set.conf")
        status, findings = compare(listed)
        self.assertEqual((status, findings[0][0]), (2, "set.conf"))
        self.assertIn("is a symbolic link", findings[0][1])


@unittest.skipUnless(gpg_runs(), "signing needs gpg and gpgv")
class VerifySetWithAKey(SignedSetCase):
    """verify-set over a set sign-set signed with a key generated for the test."""

    def setUp(self):
        super().setUp()
        status, _, stderr = self.sign()
        self.assertEqual(status, 0, stderr)
        self.stage = self.root / "build" / "acme"
        self.public = self.scratch / "acme.asc"
        self.public.write_text(self.gpg("--armor", "--export", self.primary), encoding="utf-8")

    def verify(self, *arguments: str, key=None):
        return run("verify-set", str(self.stage), "--key", str(key or self.public), *arguments)

    def test_a_signed_set_verifies_and_prints_its_primary(self):
        status, stdout, stderr = self.verify("--signer", self.primary)
        self.assertEqual((status, stdout, stderr), (0, f"{self.primary}\n", ""))
        binary = self.scratch / "acme.gpg"
        binary.write_bytes(subprocess.run(["gpg", "--export", self.primary], capture_output=True, check=True, env=self.environment).stdout)
        status, stdout, stderr = self.verify("--signer", self.primary, key=binary)
        self.assertEqual((status, stdout, stderr), (0, f"{self.primary}\n", ""), "the binary key")

    def test_another_key_and_another_signer_leave_it_unable(self):
        other = self.scratch / "other"
        other.mkdir(mode=0o700)
        environment = dict(os.environ, GNUPGHOME=str(other))
        subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key", "Other <o@example.org>",
                        "ed25519", "sign", "never"], capture_output=True, check=True, env=environment)
        exported = subprocess.run(["gpg", "--armor", "--export"], capture_output=True, check=True, env=environment).stdout
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=environment, capture_output=True)
        (self.scratch / "other.asc").write_bytes(exported)
        status, _, stderr = self.verify(key=self.scratch / "other.asc")
        self.assertEqual(status, 2, "a key that does not hold the signer leaves the check unable, as base's contract has it")
        self.assertIn("release.signature: the signature is by ", stderr)
        status, _, stderr = self.verify("--signer", "0" * 40)
        self.assertEqual(status, 2)
        self.assertIn(f"not by `--signer {'0' * 40}`", stderr)

    def test_a_changed_added_or_removed_file_and_an_edited_inventory_are_mismatches(self):
        readme = self.stage / "README.md"
        original = readme.read_bytes()
        cases = (("README.md", lambda: readme.write_bytes(original + b"changed\n"), "does not match its hash"),
                 ("extra.md", lambda: (self.stage / "extra.md").write_text("added\n", encoding="utf-8"), "is present and not listed"),
                 ("README.md", lambda: readme.unlink(), "is listed and absent"))
        for path, change, message in cases:
            change()
            status, _, stderr = self.verify()
            self.assertEqual(status, 1, path)
            self.assertIn(f"verify-set: {path}: release.inventory: {message}", stderr)
            readme.write_bytes(original)
            (self.stage / "extra.md").unlink(missing_ok=True)
        inventory = self.stage / "SHA256SUMS"
        inventory.write_text(inventory.read_text(encoding="utf-8") + "0" * 64 + "  extra.md\n", encoding="utf-8")
        status, _, stderr = self.verify()
        self.assertEqual(status, 1, "an edited inventory fails the signature")
        self.assertIn("SHA256SUMS.asc: release.signature: the signature does not match SHA256SUMS", stderr)

    def test_a_file_the_reader_refuses_or_cannot_read_exits_2_naming_it(self):
        readme = self.stage / "README.md"
        os.link(readme, self.scratch / "README.md")
        status, stdout, stderr = self.verify()
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("verify-set: README.md: release.inventory: has 2 links", stderr)
        (self.scratch / "README.md").unlink()
        if os.geteuid() != 0:
            readme.chmod(0)
            try:
                status, _, stderr = self.verify()
            finally:
                readme.chmod(0o644)
            self.assertEqual(status, 2)
            self.assertIn("verify-set: README.md: release.inventory: cannot be read: Permission denied", stderr)
            self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.verify()[0], 0, "control: the same set readable")

    def test_the_source_it_was_built_from_reproduces_it(self):
        status, stdout, stderr = self.verify("--source", str(self.root))
        self.assertEqual((status, stderr), (0, ""))
        skill = next((self.root / "sets" / "acme" / "skills").glob("*/SKILL.md"))
        skill.write_text(skill.read_text(encoding="utf-8") + "One more line.\n", encoding="utf-8")
        status, _, stderr = self.verify("--source", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn(f"verify-set: {skill.relative_to(self.root / 'sets' / 'acme').as_posix()}: release.inventory: differs from the source's build", stderr)

    def test_sigterm_during_the_source_build_leaves_no_scratch_directory(self):
        temporary = self.scratch / "tmp"
        temporary.mkdir()
        child = subprocess.Popen([sys.executable, str(TOOLS / "verify-set"), str(self.stage), "--key", str(self.public),
                                  "--source", str(self.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=dict(os.environ, TMPDIR=str(temporary)))
        deadline = time.monotonic() + 30
        while child.poll() is None and time.monotonic() < deadline and not list(temporary.glob("verify-set.*/build")):
            time.sleep(0.01)
        if child.poll() is not None:
            self.skipTest("verify-set finished before its source build was seen; the race is not deterministic here")
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=30), 128 + signal.SIGTERM)
        self.assertEqual(list(temporary.iterdir()), [])


class LicenseCheck(unittest.TestCase):
    def test_this_repository_passes_under_mit_alone(self):
        status, _, stderr = run("check-licenses", "--root", str(REPOSITORY))
        self.assertEqual((status, stderr), (0, ""))


# The scratch trees carry SPDX headers and annotations as string literals; `reuse lint` reads each as an expression
# of this file, so the class sits in an ignored block, as the fixture generator's literals do.
# REUSE-IgnoreStart
class LicenseCheckOverATree(unittest.TestCase):
    """REUSE 3.2 resolution over a scratch tree: every applicable expression is judged."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        (self.scratch / "LICENSES").mkdir()
        for identifier in ("MIT", "CC0-1.0", "GPL-3.0-only"):
            shutil.copy(FIXTURES / "LICENSES" / f"{identifier}.txt", self.scratch / "LICENSES" / f"{identifier}.txt")

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def write(self, relative: str, text: str) -> None:
        path = self.scratch / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def reuse(self, *tables: str) -> None:
        self.write("REUSE.toml", "version = 1\n" + "".join(f"[[annotations]]\n{table}\n" for table in tables))

    def check(self):
        status, _, stderr = run("check-licenses", "--root", str(self.scratch))
        return status, stderr

    def test_a_file_without_a_licence_is_refused_and_a_covered_one_is_not(self):
        self.reuse('path = "docs/**"\nSPDX-License-Identifier = "MIT"')
        self.write("docs/page.md", "# page\n")
        self.write("tool.py", "print()\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("tool.py: license.file", stderr)
        self.assertNotIn("page.md", stderr)

    def test_every_declaration_is_judged(self):
        # An annotation listing two identifiers, a file with two headers, and a sidecar.
        self.reuse('path = "docs/**"\nSPDX-License-Identifier = ["MIT", "GPL-3.0-only"]')
        self.write("docs/page.md", "# page\n")
        self.write("two.py", "# SPDX-License-Identifier: MIT\n# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        self.write("data.bin.license", "SPDX-License-Identifier: GPL-3.0-only\n")
        self.write("data.bin", "# SPDX-License-Identifier: MIT\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        for path in ("docs/page.md", "two.py", "data.bin"):
            self.assertIn(f"{path}: license.allowlist: the file's licence `GPL-3.0-only`", stderr, path)
        self.assertNotIn("data.bin.license:", stderr, "a sidecar is a declaration, not a judged file")

    def test_precedence_decides_how_a_header_and_an_annotation_combine(self):
        header = "# SPDX-License-Identifier: MIT\nprint()\n"
        gpl_header = "# SPDX-License-Identifier: GPL-3.0-only\nprint()\n"
        self.reuse('path = "closest/**"\nSPDX-License-Identifier = "GPL-3.0-only"',
                   'path = "aggregate/**"\nprecedence = "aggregate"\nSPDX-License-Identifier = "GPL-3.0-only"',
                   'path = "override/**"\nprecedence = "override"\nSPDX-License-Identifier = "MIT"')
        self.write("closest/own.py", header)
        self.write("aggregate/own.py", header)
        self.write("override/own.py", gpl_header)
        status, stderr = self.check()
        self.assertEqual(status, 1, stderr)
        self.assertNotIn("closest/own.py", stderr, "closest: the file's own header wins")
        self.assertIn("aggregate/own.py: license.allowlist", stderr, "aggregate: the annotation's GPL applies too")
        self.assertNotIn("override/own.py", stderr, "override: the annotation's MIT alone applies")
        self.reuse('path = "**"\nprecedence = "nearest"\nSPDX-License-Identifier = "MIT"')
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("REUSE.toml: license.file: `precedence = nearest`", stderr)

    def test_a_reuse_toml_outside_the_subset_refuses_the_file_whole_and_judges_none(self):
        self.write("tool.py", "# SPDX-License-Identifier: MIT\nprint()\n")
        outside_the_subset = (
            'path = [\n  "tool.py",\n]\nprecedence = "override"\nSPDX-License-Identifier = "GPL-3.0-only"\n',
            'path = "tool.py" # all tool code\nSPDX-License-Identifier = "GPL-3.0-only"\n',
            'path = "tool.py"\nextra = { a = "b" }\n',
            'path.glob = "tool.py"\n',
            'path = "tool.py"\nversion = 1\n',
            'SPDX-License-Identifier = "MIT"\n',
            'path = "tool.py"\npath = "other.py"\n',
            'path = "tool.py"\n[other]\nkey = "v"\n',
            'path = "tool.py"\nSPDX-License-Identifier = """MIT"""\n',
            'path = "a\\tb"\n',
        )
        for table in outside_the_subset:
            self.write("REUSE.toml", "version = 1\n[[annotations]]\n" + table)
            status, stderr = self.check()
            self.assertEqual(status, 1, table)
            self.assertIn("REUSE.toml: license.file: ", stderr, table)
            self.assertNotIn("check-licenses: tool.py:", stderr, "no file is judged on a REUSE.toml the reader cannot vouch for")
        self.reuse('path = ["tool.py", ]\nSPDX-License-Identifier = \'MIT\'', 'path = "lit\\\\*.txt"\nSPDX-License-Identifier = []')
        status, stderr = self.check()
        self.assertEqual((status, stderr), (0, ""), "a trailing comma, a literal string and an empty array are in the subset")

    def test_every_header_in_the_file_is_read_and_an_ignore_block_is_not(self):
        self.reuse('path = "**"\nSPDX-License-Identifier = "MIT"')
        self.write("late.py", "# SPDX-License-Identifier: MIT\n" + "# comment\n" * 19 + "# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        start, end = "REUSE-Ignore" + "Start", "REUSE-Ignore" + "End"
        self.write("blocked.py", f"# SPDX-License-Identifier: MIT\n# {start}\n# SPDX-License-Identifier: GPL-3.0-only\n# {end}\nprint()\n")
        self.write("unterminated.py", f"# SPDX-License-Identifier: MIT\n# {start}\n# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        self.write("after.py", f"# {start}\n# {end}\n# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        self.write("big.py", "# SPDX-License-Identifier: MIT\n" + "#" * (1024 * 1024) + "\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("late.py: license.allowlist: the file's licence `GPL-3.0-only`", stderr, "a header on line 21 is a declaration")
        self.assertNotIn("blocked.py", stderr)
        self.assertNotIn("unterminated.py", stderr, "an unterminated block runs to the file's end")
        self.assertIn("after.py: license.allowlist", stderr, "a header after the block is read")
        self.assertIn("big.py: license.file: is 1048608 bytes, over the 1048576", stderr, "a file over the bound is refused, not passed on its prefix")

    def test_a_copyright_only_last_table_is_the_annotation_that_applies(self):
        self.reuse('path = "**"\nprecedence = "override"\nSPDX-License-Identifier = "MIT"',
                   'path = "tool.py"\nSPDX-FileCopyrightText = "2026 Acme"')
        self.write("tool.py", "# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        self.write("other.py", "# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        self.write("bare.py", "print()\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("tool.py: license.allowlist", stderr, "closest: the file's own header applies; the earlier override no longer does")
        self.assertNotIn("other.py", stderr, "the override applies where it is the last match")
        self.assertNotIn("bare.py", stderr)
        self.reuse('path = "**"\nprecedence = "override"\nSPDX-License-Identifier = "MIT"',
                   'path = "bare.py"\nprecedence = "override"\nSPDX-FileCopyrightText = "2026 Acme"')
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("bare.py: license.file: states no licence", stderr, "an override that declares none leaves the file without one")

    def test_the_last_matching_annotation_applies_and_a_nested_reuse_toml_refuses(self):
        self.reuse('path = "**"\nSPDX-License-Identifier = "GPL-3.0-only"', 'path = "src/**"\nSPDX-License-Identifier = "MIT"')
        self.write("src/a.py", "print()\n")
        self.write("other.py", "print()\n")
        self.write("vendor/REUSE.toml", 'version = 1\n[[annotations]]\npath = "**"\nSPDX-License-Identifier = "MIT"\n')
        self.write("vendor/lib.py", "print()\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertNotIn("src/a.py", stderr)
        self.assertIn("other.py: license.allowlist", stderr)
        self.assertIn("vendor/lib.py: license.file: lies under `vendor/REUSE.toml`", stderr)
        self.assertIn("vendor/REUSE.toml: license.file: lies under", stderr)

    def test_globs_follow_reuse(self):
        self.reuse('path = "src/*"\nSPDX-License-Identifier = "MIT"', 'path = "docs/**/*.md"\nSPDX-License-Identifier = "MIT"',
                   'path = "lit\\\\*.txt"\nSPDX-License-Identifier = "MIT"', 'path = "one/?.py"\nSPDX-License-Identifier = "MIT"')
        self.write("src/x.py", "print()\n")
        self.write("src/vendor/proprietary.py", "print()\n")
        self.write("docs/a.md", "# a\n")
        self.write("docs/deep/er/b.md", "# b\n")
        self.write("lit*.txt", "literal star\n")
        self.write("lita.txt", "not matched\n")
        self.write("one/a.py", "print()\n")
        self.write("one/ab.py", "print()\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        for refused in ("src/vendor/proprietary.py", "lita.txt", "one/ab.py"):
            self.assertIn(f"{refused}: license.file", stderr, refused)
        for covered in ("src/x.py", "docs/a.md", "docs/deep/er/b.md", "lit*.txt", "one/a.py"):
            self.assertNotIn(covered + ":", stderr, covered)
        for glob in ("a" * 257, "src**"):
            self.reuse(f'path = "{glob}"\nSPDX-License-Identifier = "MIT"')
            status, stderr = self.check()
            self.assertEqual(status, 1)
            self.assertIn("REUSE.toml: license.file: annotations table 1: path `", stderr, "a glob outside the grammar refuses the file whole")
            self.assertNotIn("check-licenses: src/x.py", stderr)

    def test_an_exception_accepts_its_identifier_for_the_files_its_glob_matches_alone(self):
        self.reuse('path = "**"\nSPDX-License-Identifier = "MIT"')
        self.write("vendor/a.sh", "# SPDX-License-Identifier: GPL-3.0-only\n")
        self.write("vendor/deep/b.el", ";; SPDX-License-Identifier: GPL-3.0-only\n")
        self.write("tool.py", "# SPDX-License-Identifier: GPL-3.0-only\n")

        def check_with(*exceptions: str) -> str:
            arguments = [argument for exception in exceptions for argument in ("--exception", exception)]
            status, _, stderr = run("check-licenses", "--root", str(self.scratch), *arguments)
            self.assertEqual(status, 1, exceptions)
            return stderr

        stderr = check_with()
        for path in ("vendor/a.sh", "vendor/deep/b.el", "tool.py"):
            self.assertIn(f"{path}: license.allowlist", stderr, path)
        stderr = check_with("vendor/*=GPL-3.0-only")
        self.assertNotIn("vendor/a.sh", stderr)
        self.assertIn("vendor/deep/b.el: license.allowlist", stderr, "a single star does not reach a subdirectory")
        stderr = check_with("vendor/**=GPL-3.0-only")
        self.assertNotIn("vendor/", stderr)
        self.assertIn("tool.py: license.allowlist", stderr, "a file outside the glob is judged on the list in force")

    def test_a_licence_text_is_held_to_the_text_as_published(self):
        self.reuse('path = "**"\nSPDX-License-Identifier = "MIT"')
        self.write("tool.py", "print()\n")
        status, stdout, stderr = run("check-licenses", "--root", str(self.scratch))
        self.assertEqual((status, stderr), (0, ""), "the published texts pass")
        self.assertIn("every file is under MIT (permissive)", stdout)
        published = (self.scratch / "LICENSES" / "MIT.txt").read_text(encoding="utf-8")
        for edited in (published.replace("<year> <copyright holders>", "2026 Acme"), published.replace("\n", " \n", 1),
                       published.rstrip("\n")):
            (self.scratch / "LICENSES" / "MIT.txt").write_text(edited, encoding="utf-8")
            status, stderr = self.check()
            self.assertEqual(status, 0, "a text that differs warns; format 1 sets passed without the comparison")
            self.assertIn("check-licenses: warning: LICENSES/MIT.txt: license.text-modified: is not the `MIT` text as published", stderr,
                          "a filled holder line, a changed byte and a dropped newline each warn")

    def test_an_unpinned_text_warns_until_publisher_conf_declares_its_digest(self):
        self.reuse('path = "**"\nSPDX-License-Identifier = "MIT"')
        self.write("LICENSES/Beerware.txt", "A licence text the tools hold no digest of.\n")
        status, stderr = self.check()
        self.assertEqual(status, 0, stderr)
        self.assertIn("check-licenses: warning: LICENSES/Beerware.txt: license.text-unpinned: ", stderr)
        digest = hashlib.sha256(b"A licence text the tools hold no digest of.\n").hexdigest()
        self.write("publisher.conf", f"license_text_digests=[Beerware:{digest}]\n")
        status, stderr = self.check()
        self.assertEqual((status, stderr), (0, ""), "a declared digest that matches checks the text")
        self.write("publisher.conf", f"license_text_digests=[Beerware:{'0' * 64}]\n")
        status, stderr = self.check()
        self.assertEqual(status, 0, stderr)
        self.assertIn("warning: LICENSES/Beerware.txt: license.text-modified: is not the `Beerware` text as published", stderr)
        self.write("publisher.conf", "license_text_digests=[Beerware]\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("publisher.conf: repo.publisher-conf: `license_text_digests` item `Beerware` is not `<identifier>:<sha256>`", stderr)

    def test_the_summary_names_each_licence_with_its_class(self):
        shutil.copy(FIXTURES / "LICENSES" / "CC-BY-NC-4.0.txt", self.scratch / "LICENSES" / "CC-BY-NC-4.0.txt")
        self.reuse('path = "**"\nSPDX-License-Identifier = "MIT"', 'path = "data/**"\nSPDX-License-Identifier = "CC-BY-NC-4.0"')
        self.write("data/table.csv", "a,b\n")
        self.write("tool.py", "print()\n")
        status, stdout, stderr = run("check-licenses", "--root", str(self.scratch), "--allow", "CC-BY-NC-4.0")
        self.assertEqual((status, stderr), (0, ""), "the allowlist decides; a class is not a finding")
        self.assertIn("every file is under CC-BY-NC-4.0 (limited-use), MIT (permissive)", stdout)

    def test_a_file_the_reader_refuses_or_cannot_open_is_refused_not_judged_on_an_annotation(self):
        self.reuse('path = "**"\nSPDX-License-Identifier = "MIT"')
        self.write("linked.py", "# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        os.link(self.scratch / "linked.py", self.scratch / "alias.py")
        os.symlink("linked.py", self.scratch / "link.py")
        self.write("fine.py", "print()\n")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("linked.py: license.file: `linked.py` has 2 links", stderr, "a second hard link is a refusal, not a header the annotation stands in for")
        self.assertIn("alias.py: license.file: `alias.py` has 2 links", stderr)
        self.assertNotIn("link.py:", stderr, "a symbolic link is a path, not content, and is not judged")
        self.assertNotIn("fine.py", stderr)
        (self.scratch / "alias.py").unlink()
        self.write("secret.py", "# SPDX-License-Identifier: GPL-3.0-only\nprint()\n")
        self.write("data.bin.license", "SPDX-License-Identifier: GPL-3.0-only\n")
        self.write("data.bin", "# SPDX-License-Identifier: MIT\n")
        (self.scratch / "secret.py").chmod(0)
        (self.scratch / "data.bin.license").chmod(0)
        if os.access(self.scratch / "secret.py", os.R_OK):
            self.skipTest("this host reads a file with no permission bits")
        status, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("linked.py: license.allowlist: the file's licence `GPL-3.0-only`", stderr, "with one link again, the header is judged")
        self.assertIn("secret.py: license.file: `secret.py` cannot be read (Permission denied)", stderr)
        self.assertIn("data.bin: license.file: `data.bin.license` cannot be read (Permission denied)", stderr, "an unreadable sidecar refuses the file it declares for")
# REUSE-IgnoreEnd


class SignoffCheck(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Ada Example")
        self.git("config", "user.email", "ada@example.org")
        (self.repo / "a").write_text("a\n")
        self.git("add", "a")
        self.git("commit", "-q", "-m", "base")

    def tearDown(self):
        shutil.rmtree(self.repo)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True).stdout

    def commit(self, name: str, *flags: str) -> None:
        (self.repo / name).write_text(name + "\n")
        self.git("add", name)
        self.git("commit", "-q", "-m", f"add {name}", *flags)

    def test_signed_commits_pass_and_unsigned_fail(self):
        self.commit("b", "-s")
        self.commit("c", "-s")
        status, stdout, stderr = run("check-signoff", "--repo", str(self.repo), "--range", "main~2..main")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("2 commit(s)", stdout)
        self.commit("d")
        status, _, stderr = run("check-signoff", "--repo", str(self.repo), "--range", "main~3..main")
        self.assertEqual(status, 1)
        self.assertIn("add d", stderr)
        self.assertNotIn("add c", stderr)

    def test_a_sign_off_by_another_person_does_not_count(self):
        self.commit("b", "--trailer", "Signed-off-by: Someone Else <else@example.org>")
        status, _, stderr = run("check-signoff", "--repo", str(self.repo), "--range", "main~1..main")
        self.assertEqual(status, 1)
        self.assertIn("no Signed-off-by matching the author", stderr)

    def test_a_body_phrase_is_not_a_trailer(self):
        (self.repo / "b").write_text("b\n")
        self.git("add", "b")
        self.git("commit", "-q", "-m", "add b\n\nSigned-off-by: Ada Example <ada@example.org> is what I meant to add\n\nMore body text.")
        status, _, _ = run("check-signoff", "--repo", str(self.repo), "--range", "main~1..main")
        self.assertEqual(status, 1)

    def test_an_unreadable_or_empty_range_fails(self):
        status, _, _ = run("check-signoff", "--repo", str(self.repo), "--range", "nope..main")
        self.assertEqual(status, 2)
        status, _, _ = run("check-signoff", "--repo", str(self.repo), "--range", "main..main")
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
