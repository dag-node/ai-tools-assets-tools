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

from key_value_config import parse_key_value_text  # noqa: E402


def run_validate(set_directory: pathlib.Path, publisher: str, profile: str = "source", license_texts=None, licenses=None,
                 publisher_conf: pathlib.Path = None):
    command = [sys.executable, str(VALIDATE), "--set-directory", str(set_directory), "--publisher", publisher, "--profile", profile]
    for directory in license_texts or [FIXTURES]:
        command += ["--license-texts", str(directory)]
    if licenses is not None:
        command += ["--licenses", licenses]
    if publisher_conf is not None:
        command += ["--publisher-conf", str(publisher_conf)]
    completed = subprocess.run(command, capture_output=True, text=True)
    lines = completed.stderr.strip().splitlines() if completed.stderr.strip() else []
    refusals = [line for line in lines if ": warning: " not in line]
    warnings = [line for line in lines if ": warning: " in line]
    return completed.returncode, refusals, warnings


def fixture_cases():
    for conf_path in sorted(FIXTURES.glob("*/*/fixture.conf")):
        conf = parse_key_value_text(conf_path.read_text(encoding="utf-8"))
        set_directory = next(path for path in conf_path.parent.iterdir() if path.is_dir())
        yield conf_path.parent, conf, set_directory


class CommittedFixtures(unittest.TestCase):
    def test_every_fixture_meets_its_expectation(self):
        cases = list(fixture_cases())
        self.assertGreater(len(cases), 40, "the fixtures were not generated; run tests/fixture_generator.py generate")
        for fixture_directory, conf, set_directory in cases:
            with self.subTest(fixture=fixture_directory.relative_to(FIXTURES)):
                license_texts = [(fixture_directory / conf.get("license_texts")).resolve()] if conf.has("license_texts") else []
                publisher_conf = (fixture_directory / conf.get("publisher_conf")).resolve() if conf.has("publisher_conf") else None
                status, refusals, warnings = run_validate(set_directory, conf.get("publisher"), conf.get("profile", "source"),
                                                          license_texts, conf.get("licenses") if conf.has("licenses") else None,
                                                          publisher_conf)
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

    def assert_one_budget_finding(self, fragment: str):
        """A tripped budget is one `file.size` finding at the set root naming it, and no later rule reports."""
        status, refusals, warnings = run_validate(self.set_directory, "acme")
        self.assertEqual(status, 1)
        self.assertEqual(len(refusals), 1, refusals[:3])
        self.assertEqual(warnings, [])
        self.assertIn(": acme: file.size: ", refusals[0])
        self.assertIn(fragment, refusals[0])

    def test_too_many_entries_in_one_directory(self):
        for index in range(2001):
            (self.skill / "references" / f"page-{index}.md").write_text("# p\n", encoding="utf-8")
        self.assert_one_budget_finding("more than 2000 entries")

    def test_too_many_files(self):
        for directory in range(3):
            (self.skill / "references" / f"part-{directory}").mkdir()
            for index in range(800):
                (self.skill / "references" / f"part-{directory}" / f"page-{index}.md").write_text("# p\n", encoding="utf-8")
        self.assert_one_budget_finding("more than 2000 files")

    def test_too_many_directories(self):
        for index in range(501):
            (self.skill / "references" / f"part-{index}").mkdir()
        self.assert_one_budget_finding("more than 500 directories")

    def test_too_deep(self):
        deep = self.skill / "references"
        for _ in range(40):
            deep = deep / "d"
        deep.mkdir(parents=True)
        self.assert_one_budget_finding("levels deep")

    def test_too_many_bytes_in_all(self):
        for index in range(65):
            (self.skill / "references" / f"big-{index}.md").write_bytes(b"x" * (1024 * 1024))
        self.assert_one_budget_finding("more than 67108864 bytes")

    def test_a_symlinked_set_root_is_refused(self):
        link = self.scratch / "linked"
        link.symlink_to(self.set_directory, target_is_directory=True)
        status, refusals, _ = run_validate(link, "acme")
        self.assertEqual(status, 1)
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn(": linked: file.symlink: ", refusals[0])

    def test_a_symlinked_directory_inside_the_set_is_refused_unread(self):
        outside = self.scratch / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("---\nname: acme-outside\ndescription: x\n---\n!`date`\n", encoding="utf-8")
        (self.set_directory / "skills" / "acme-outside").symlink_to(outside, target_is_directory=True)
        status, refusals, _ = run_validate(self.set_directory, "acme")
        self.assertEqual(status, 1)
        self.assertTrue(all(": file.symlink: " in line for line in refusals), refusals)
        self.assertFalse(any("dynamic-injection" in line for line in refusals), "the target was not read")

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


class DynamicInjectionAdmission(unittest.TestCase):
    """A publisher.conf value the validator does not read as `yes` or `no` is reported and does not admit."""

    def test_an_unknown_value_is_reported_and_does_not_admit(self):
        fixture = FIXTURES / "pass" / "acme.dynamic-injection-declared"
        with tempfile.TemporaryDirectory() as scratch:
            publisher_conf = pathlib.Path(scratch) / "publisher.conf"
            text = (fixture / "publisher.conf").read_text(encoding="utf-8")
            self.assertIn("allow_dynamic_injection=yes\n", text, "the control fixture admits the capability")
            publisher_conf.write_text(text.replace("allow_dynamic_injection=yes", "allow_dynamic_injection=true"), encoding="utf-8")
            control, _, _ = run_validate(fixture / "acme", "acme", publisher_conf=fixture / "publisher.conf")
            status, refusals, _ = run_validate(fixture / "acme", "acme", publisher_conf=publisher_conf)
        self.assertEqual(control, 0, "the fixture passes with the value it ships")
        self.assertEqual(status, 1, refusals)
        self.assertTrue(any(": repo.publisher-conf: " in line and "allow_dynamic_injection=true" in line for line in refusals), refusals)
        self.assertTrue(any(": body.dynamic-injection: " in line and "does not admit" in line for line in refusals), refusals)


if __name__ == "__main__":
    unittest.main()
