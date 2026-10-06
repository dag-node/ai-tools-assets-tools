# SPDX-License-Identifier: MIT
"""Drives each command over a publisher repository built from the passing fixture in a temporary directory.

The repository-level rules (publisher.conf, the marketplace, the stale-manifest check), the scaffolding commands, the
builder's inventory and zip, link-set's placement and removal, check-licenses over a tree and check-signoff over a
synthetic history each have a case here; the set-level rules are tests/test_fixtures.py's.
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
FIXTURES = REPOSITORY / "fixtures"
TOOLS = REPOSITORY / "tools"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TOOLS / "lib"))

from fixture_generator import PUBLISHER_CONF  # noqa: E402


def run(command: str, *arguments: str, cwd=None):
    completed = subprocess.run([sys.executable, str(TOOLS / command), *arguments], capture_output=True, text=True, cwd=cwd)
    return completed.returncode, completed.stdout, completed.stderr


class PublisherRepository(unittest.TestCase):
    """A publisher repository composed from fixtures/pass/acme."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "acme-assets"
        (self.root / "sets").mkdir(parents=True)
        shutil.copytree(FIXTURES / "pass" / "acme" / "acme", self.root / "sets" / "acme")
        shutil.copytree(FIXTURES / "LICENSES", self.root / "LICENSES")
        (self.root / "LICENSE").write_text((FIXTURES / "LICENSES" / "MIT.txt").read_text(encoding="utf-8"), encoding="utf-8")
        (self.root / "publisher.conf").write_text(PUBLISHER_CONF.format(publisher="acme"), encoding="utf-8")
        status, _, stderr = run("sync-manifests", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)

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

    def test_a_publisher_conf_without_its_keys_is_refused(self):
        (self.root / "publisher.conf").write_text("publisher=acme\n", encoding="utf-8")
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual(status, 1)
        self.assertIn("repo.publisher-conf", stderr)

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
        status, _, stderr = run("new-asset", "acme-dotnet", "skill", "acme-dotnet-ef-migrations", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        status, _, stderr = run("new-asset", "acme-dotnet", "subagent", "acme-dotnet-reviewer", "--root", str(self.root))
        self.assertEqual(status, 0, stderr)
        status, _, stderr = run("validate", "--root", str(self.root))
        self.assertEqual((status, stderr), (0, ""))

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
        archive = self.root / "dist" / "ai-tools-assets-acme-0.1.0.zip"
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as opened:
            names = set(opened.namelist())
        self.assertIn("plugin.json", names, "the plugin root is at the top of the archive")
        self.assertIn("SHA256SUMS", names)
        self.assertEqual((self.root / "dist" / "ai-tools-assets-acme-0.1.0.zip.sha256").read_text(encoding="utf-8").split()[0],
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


class LicenseCheck(unittest.TestCase):
    def test_this_repository_passes_with_the_formatters_exception(self):
        status, _, stderr = run("check-licenses", "--root", str(REPOSITORY), "--exception", "formatters/**=AGPL-3.0-only")
        self.assertEqual((status, stderr), (0, ""))

    def test_the_exception_is_needed_and_a_single_star_does_not_reach_a_subdirectory(self):
        status, _, stderr = run("check-licenses", "--root", str(REPOSITORY))
        self.assertEqual(status, 1)
        self.assertIn("license.allowlist", stderr)
        status, _, stderr = run("check-licenses", "--root", str(REPOSITORY), "--exception", "formatters/*=AGPL-3.0-only")
        self.assertEqual(status, 1)
        self.assertIn("formatters/emacs/ai-tools-fill.el: license.allowlist", stderr)
        self.assertNotIn("formatters/format.sh", stderr)


class LicenseCheckOverATree(unittest.TestCase):
    """REUSE 3.2 resolution over a scratch tree: every applicable expression is judged."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        (self.scratch / "LICENSES").mkdir()
        for identifier in ("MIT", "CC0-1.0"):
            shutil.copy(FIXTURES / "LICENSES" / f"{identifier}.txt", self.scratch / "LICENSES" / f"{identifier}.txt")
        (self.scratch / "LICENSES" / "GPL-3.0-only.txt").write_text("GPL text\n", encoding="utf-8")

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
