# SPDX-License-Identifier: MIT
"""Drives the formatters a checkout can prove without Emacs: the reflow gate over a scratch repository, and the table
aligner's block boundaries over strings and files.

`formatters/verify-reflow.py` is run as a command, since its exit status is what a reflow commit is gated on;
`formatters/align-tables.py` is imported for `realign()` and run once as a command.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
FORMATTERS = REPOSITORY / "formatters"
sys.path.insert(0, str(FORMATTERS))


def load(script: str):
    """Import a formatter whose file name carries a hyphen, as a module."""
    name = script.replace("-", "_").removesuffix(".py")
    loader = importlib.machinery.SourceFileLoader(name, str(FORMATTERS / script))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run(script: str, *arguments: str):
    completed = subprocess.run([sys.executable, str(FORMATTERS / script), *arguments], capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


class VerifyReflow(unittest.TestCase):
    """One page committed, then reflowed in the tree."""

    ORIGINAL = "# Page\n\nOne two three four\nfive six.\n"
    REFLOWED = "# Page\n\nOne two\nthree four five\nsix.\n"

    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Ada Example")
        self.git("config", "user.email", "ada@example.org")
        (self.repo / "page.md").write_text(self.ORIGINAL, encoding="utf-8")
        self.git("add", "page.md")
        self.git("commit", "-q", "-m", "base")
        (self.repo / "page.md").write_text(self.REFLOWED, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.repo)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True).stdout

    def verify(self, *arguments: str):
        return run("verify-reflow.py", "--repo", str(self.repo), *arguments)

    def test_a_pure_reflow_passes_and_a_changed_word_fails(self):
        status, stdout, stderr = self.verify("--base", "HEAD", "--", "page.md")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("1 pure reflow(s), 0 failure(s), 0 skipped", stdout)
        (self.repo / "page.md").write_text(self.REFLOWED.replace("five", "FIVE"), encoding="utf-8")
        status, stdout, _ = self.verify("--base", "HEAD", "--", "page.md")
        self.assertEqual(status, 1)
        self.assertIn("FAIL page.md: token differs", stdout)

    def test_a_base_that_does_not_exist_is_an_error_not_a_run_of_skipped_paths(self):
        plain = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plain)
        for arguments in (["--repo", str(self.repo), "--base", "no-such-revision"],
                          ["--repo", str(self.repo), "--against", str(self.repo / "no-such-directory")],
                          ["--repo", str(plain), "--base", "HEAD"]):
            status, stdout, stderr = run("verify-reflow.py", *arguments, "--", "page.md")
            self.assertEqual(status, 1, arguments)
            self.assertIn("verify-reflow: ", stderr, arguments)
            self.assertNotIn("SKIP", stdout, arguments)

    def test_a_path_the_base_does_not_hold_is_skipped_and_one_the_tree_does_not_hold_fails(self):
        (self.repo / "new.md").write_text("# New\n", encoding="utf-8")
        status, stdout, _ = self.verify("--base", "HEAD", "--", "page.md", "new.md")
        self.assertEqual(status, 0)
        self.assertIn("SKIP new.md: the base does not hold new.md", stdout)
        self.assertIn("1 pure reflow(s), 0 failure(s), 1 skipped", stdout)
        status, stdout, _ = self.verify("--base", "HEAD", "--", "missing.md")
        self.assertEqual(status, 1)
        self.assertIn("FAIL missing.md: unreadable, ", stdout)

    def test_against_reads_the_base_copies_under_a_directory(self):
        base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base)
        (base / "page.md").write_text(self.ORIGINAL, encoding="utf-8")
        status, stdout, stderr = self.verify("--against", str(base), "--", "page.md")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("1 pure reflow(s)", stdout)


class AlignTables(unittest.TestCase):
    TABLE = "# A | B\n# --+--\n"
    ALIGNED = "# A | B\n# --+---\n"
    FENCE = "```\n# x | y\n# --+--\n```\n"

    def setUp(self):
        self.align = load("align-tables.py")

    def test_a_fence_ends_a_table_and_what_it_encloses_is_left_alone(self):
        self.assertEqual(self.align.realign(self.TABLE), (self.ALIGNED, [(1, 2)]))
        self.assertEqual(self.align.realign(self.TABLE + self.FENCE), (self.ALIGNED + self.FENCE, [(1, 2)]),
                         "a fence right after the table ends it, and the rows inside the fence are not read")
        self.assertEqual(self.align.realign(self.FENCE + self.TABLE), (self.FENCE + self.ALIGNED, [(5, 6)]),
                         "a table after a fence is read")
        self.assertEqual(self.align.realign(self.TABLE.rstrip("\n")), (self.ALIGNED.rstrip("\n"), [(1, 2)]),
                         "a table at the end of the file is read")

    def test_the_command_goes_on_to_the_next_file(self):
        scratch = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch)
        (scratch / "fenced.sh").write_text(self.TABLE + self.FENCE, encoding="utf-8")
        (scratch / "plain.sh").write_text(self.TABLE, encoding="utf-8")
        status, stdout, stderr = run("align-tables.py", "check", str(scratch / "fenced.sh"), str(scratch / "plain.sh"))
        self.assertEqual((status, stderr), (1, ""))
        self.assertIn("fenced.sh:1: table cells do not line up (lines 1-2)", stdout)
        self.assertIn("plain.sh:1: table cells do not line up (lines 1-2)", stdout)
        status, _, stderr = run("align-tables.py", "fix", str(scratch / "fenced.sh"))
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual((scratch / "fenced.sh").read_text(encoding="utf-8"), self.ALIGNED + self.FENCE)


if __name__ == "__main__":
    unittest.main()
