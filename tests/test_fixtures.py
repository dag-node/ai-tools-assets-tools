# SPDX-License-Identifier: MIT
"""Runs tools/validate over every conformance fixture and holds the outcome to the fixture's own fixture.conf.

A passing fixture validates with no finding; a warning fixture exits 0 with its rule among the warnings; a failing
fixture exits 1 and every refusing finding carries its rule, so a fixture fails on that rule alone. The rules whose
trees git does not carry -- a hard link, a special file, an over-size file, a file name with a newline -- are built here
in a temporary directory from the passing fixture.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
FIXTURES = REPOSITORY / "fixtures"
VALIDATE = REPOSITORY / "tools" / "validate"
sys.path.insert(0, str(REPOSITORY / "tools" / "lib"))

from key_value_config import read_key_value_file  # noqa: E402


def run_validate(set_directory: pathlib.Path, publisher: str, profile: str = "source", license_texts=None, licenses=None):
    command = [sys.executable, str(VALIDATE), "--set-directory", str(set_directory), "--publisher", publisher, "--profile", profile]
    for directory in license_texts or [FIXTURES]:
        command += ["--license-texts", str(directory)]
    if licenses is not None:
        command += ["--licenses", licenses]
    completed = subprocess.run(command, capture_output=True, text=True)
    lines = completed.stderr.strip().splitlines() if completed.stderr.strip() else []
    refusals = [line for line in lines if ": warning: " not in line]
    warnings = [line for line in lines if ": warning: " in line]
    return completed.returncode, refusals, warnings


def fixture_cases():
    for conf_path in sorted(FIXTURES.glob("*/*/fixture.conf")):
        conf = read_key_value_file(conf_path)
        set_directory = next(path for path in conf_path.parent.iterdir() if path.is_dir())
        yield conf_path.parent, conf, set_directory


class CommittedFixtures(unittest.TestCase):
    def test_every_fixture_meets_its_expectation(self):
        cases = list(fixture_cases())
        self.assertGreater(len(cases), 40, "the fixtures were not generated; run tests/fixture_generator.py generate")
        for fixture_directory, conf, set_directory in cases:
            with self.subTest(fixture=fixture_directory.relative_to(FIXTURES)):
                license_texts = [(fixture_directory / conf.get("license_texts")).resolve()] if conf.has("license_texts") else []
                status, refusals, warnings = run_validate(set_directory, conf.get("publisher"), conf.get("profile", "source"),
                                                          license_texts, conf.get("licenses") if conf.has("licenses") else None)
                expect, rule = conf.get("expect"), conf.get("rule")
                if expect == "pass":
                    self.assertEqual((status, refusals, warnings), (0, [], []))
                elif expect == "warn":
                    self.assertEqual((status, refusals), (0, []), warnings)
                    self.assertTrue(any(f": {rule}: " in line for line in warnings), warnings)
                else:
                    self.assertEqual(status, 1, refusals + warnings)
                    self.assertTrue(refusals, "a failing fixture refuses")
                    for line in refusals:
                        self.assertIn(f": {rule}: ", line, f"a finding outside the fixture's rule: {line}")

    def test_generator_output_is_committed(self):
        completed = subprocess.run([sys.executable, str(TESTS / "fixture_generator.py"), "check"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class UncommittableFixtures(unittest.TestCase):
    """Trees git does not carry, built from the passing fixture in a temporary directory."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.set_directory = self.scratch / "acme"
        shutil.copytree(FIXTURES / "pass" / "acme" / "acme", self.set_directory, symlinks=True)
        self.skill = self.set_directory / "skills" / "acme-pdf-processing"

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def assert_refuses_alone(self, rule: str):
        status, refusals, _ = run_validate(self.set_directory, "acme")
        self.assertEqual(status, 1, refusals)
        self.assertTrue(refusals)
        for line in refusals:
            self.assertIn(f": {rule}: ", line, line)

    def test_hard_link(self):
        os.link(self.skill / "references" / "formats.md", self.skill / "references" / "formats-copy.md")
        self.assert_refuses_alone("file.hardlink")

    def test_special_file(self):
        try:
            os.mkfifo(self.skill / "assets-pipe")
        except PermissionError:
            self.skipTest("this host denies creating a special file; the runner does not")
        status, refusals, _ = run_validate(self.set_directory, "acme")
        self.assertEqual(status, 1)
        self.assertTrue(any(": file.special: " in line for line in refusals), refusals)

    def test_over_size_file(self):
        (self.skill / "references" / "big.md").write_bytes(b"x" * (1024 * 1024 + 1))
        self.assert_refuses_alone("file.size")

    def test_file_name_with_newline(self):
        (self.skill / "references" / "two\nlines.md").write_text("# x\n", encoding="utf-8")
        self.assert_refuses_alone("file.name")

    def test_too_many_files(self):
        for index in range(2001):
            (self.skill / "references" / f"page-{index}.md").write_text("# p\n", encoding="utf-8")
        status, refusals, _ = run_validate(self.set_directory, "acme")
        self.assertEqual(status, 1)
        self.assertTrue(any(": file.size: " in line and "files" in line for line in refusals), refusals[:3])

    def test_release_profile_accepts_a_correct_inventory(self):
        import hashlib
        lines = []
        for path in sorted(p for p in self.set_directory.rglob("*") if p.is_file()):
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(self.set_directory).as_posix()}")
        (self.set_directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
        status, refusals, warnings = run_validate(self.set_directory, "acme", profile="release")
        self.assertEqual((status, refusals, warnings), (0, [], []))

    def test_source_profile_refuses_a_committed_inventory(self):
        (self.set_directory / "SHA256SUMS").write_text("", encoding="utf-8")
        self.assert_refuses_alone("file.reserved-name")


if __name__ == "__main__":
    unittest.main()
