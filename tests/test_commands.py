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
        status, _, stderr = run("check-licenses", "--root", str(REPOSITORY), "--exception", "formatters/*=AGPL-3.0-only")
        self.assertEqual((status, stderr), (0, ""))

    def test_the_exception_is_needed(self):
        status, _, stderr = run("check-licenses", "--root", str(REPOSITORY))
        self.assertEqual(status, 1)
        self.assertIn("license.allowlist", stderr)

    def test_a_file_without_a_licence_is_refused(self):
        scratch = pathlib.Path(tempfile.mkdtemp())
        try:
            (scratch / "LICENSES").mkdir()
            shutil.copy(FIXTURES / "LICENSES" / "MIT.txt", scratch / "LICENSES" / "MIT.txt")
            (scratch / "REUSE.toml").write_text('version = 1\n[[annotations]]\npath = "docs/**"\nSPDX-License-Identifier = "MIT"\n', encoding="utf-8")
            (scratch / "docs").mkdir()
            (scratch / "docs" / "page.md").write_text("# page\n", encoding="utf-8")
            (scratch / "tool.py").write_text("print()\n", encoding="utf-8")
            status, _, stderr = run("check-licenses", "--root", str(scratch))
            self.assertEqual(status, 1)
            self.assertIn("tool.py: license.file", stderr)
            self.assertNotIn("page.md", stderr)
        finally:
            shutil.rmtree(scratch)


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
