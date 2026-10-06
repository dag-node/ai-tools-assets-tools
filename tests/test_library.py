# SPDX-License-Identifier: MIT
"""Unit tests for the readers under tools/lib: the KEY=value grammar, the frontmatter subset, SPDX expressions, and
the walk's reads counted in-process, which a subprocess cannot show."""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "tools" / "lib"))

import asset_format as fmt  # noqa: E402
import safe_read  # noqa: E402
import set_validation  # noqa: E402
from findings import FindingCollector  # noqa: E402
from frontmatter import split_frontmatter  # noqa: E402
from key_value_config import parse_key_value_text, parse_list_value  # noqa: E402
from spdx_expression import evaluate_expression  # noqa: E402

FIXTURES = REPOSITORY / "fixtures"


class KeyValueGrammar(unittest.TestCase):
    def test_reads_the_grammar_base_reads(self):
        document = parse_key_value_text('# c\nformat=1\nname = core\nsummary="Community baseline" # x\nempty=\nq="[a]"\n')
        self.assertEqual(document.values, {"format": "1", "name": "core", "summary": "Community baseline", "empty": "", "q": "[a]"})
        self.assertTrue(document.has("empty"))
        self.assertEqual(document.list_value("q"), ((), "a bracketed list is written without quotes around it"))
        self.assertEqual(document.syntax_errors(), [])

    def test_reports_a_line_without_equals_and_a_repeated_key(self):
        document = parse_key_value_text("a=1\nbad line\na=2\n")
        self.assertEqual(document.get("a"), "2")
        self.assertEqual(len(document.syntax_errors()), 2)

    def test_lists(self):
        self.assertEqual(parse_list_value("[a, b]"), (("a", "b"), None))
        self.assertEqual(parse_list_value("a, b c"), (("a", "b", "c"), None))
        self.assertEqual(parse_list_value("[]"), ((), None))
        self.assertEqual(parse_list_value("[a")[0], ())
        self.assertEqual(parse_list_value("[a, 'b']")[0], ())


class FrontmatterSubset(unittest.TestCase):
    def test_reads_scalars_maps_and_lists(self):
        document, body = split_frontmatter('---\nname: pdf\ndescription: "Extract text. Use when x."\nmetadata:\n  ai-tools-libs: python\ntools: [Read, Grep]\nskills:\n  - a\n---\n# Body\n')
        self.assertEqual(document.errors, [])
        self.assertEqual(document.values["description"], "Extract text. Use when x.")
        self.assertEqual(document.values["metadata"], {"ai-tools-libs": "python"})
        self.assertEqual(document.values["tools"], ["Read", "Grep"])
        self.assertEqual(document.values["skills"], ["a"])
        self.assertEqual(body, "# Body\n")

    def test_refuses_what_is_outside_the_subset(self):
        document, _ = split_frontmatter("---\nname: x\nname: y\ndesc: |\n  block\n\tbad: tab\nnested:\n  a:\n    b: c\n")
        joined = "\n".join(document.errors)
        for fragment in ("given again", "opening with `|`", "tab in the indentation", "has no value", "does not close"):
            self.assertIn(fragment, joined)

    def test_absent_frontmatter(self):
        document, body = split_frontmatter("# Title\n")
        self.assertFalse(document.present)
        self.assertEqual(body, "# Title\n")


class SpdxExpressions(unittest.TestCase):
    allowlist = ("MIT", "Apache-2.0", "BSD-3-Clause")

    def test_policy_table(self):
        self.assertTrue(evaluate_expression("MIT OR Apache-2.0", self.allowlist).is_allowed)
        self.assertTrue(evaluate_expression("MIT AND BSD-3-Clause", self.allowlist).is_allowed)
        self.assertTrue(evaluate_expression("(MIT OR Apache-2.0) AND BSD-3-Clause", self.allowlist).is_allowed)
        refused = evaluate_expression("MIT OR GPL-3.0-only", self.allowlist)
        self.assertTrue(refused.is_well_formed)
        self.assertEqual(refused.outside_allowlist, ["GPL-3.0-only"])

    def test_unsupported_forms(self):
        for expression in ("LicenseRef-Custom", "Apache-2.0 WITH LLVM-exception", "GPL-2.0+", "NONE", "NOASSERTION"):
            evaluation = evaluate_expression(expression, self.allowlist)
            self.assertFalse(evaluation.is_well_formed, expression)
            self.assertIsNone(evaluation.syntax_error, expression)

    def test_malformed(self):
        for expression in ("", "(MIT AND", "MIT MIT", "AND MIT", "MIT)"):
            self.assertIsNotNone(evaluate_expression(expression, self.allowlist).syntax_error, expression)


class SafeRead(unittest.TestCase):
    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.root_fd = os.open(self.scratch, os.O_RDONLY | os.O_DIRECTORY)

    def tearDown(self):
        os.close(self.root_fd)
        shutil.rmtree(self.scratch)

    def test_reads_whole_and_truncated_with_the_digest_over_the_whole_alone(self):
        (self.scratch / "small").write_bytes(b"abc")
        (self.scratch / "large").write_bytes(b"x" * 11)
        small = safe_read.read_file("small", self.root_fd, 10)
        self.assertEqual((small.data, small.size, small.truncated), (b"abc", 3, False))
        self.assertEqual(small.digest, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        large = safe_read.read_file("large", self.root_fd, 10)
        self.assertEqual((len(large.data), large.size, large.truncated, large.digest), (10, 11, True, None))

    def test_refuses_a_link_a_hard_link_and_a_special_file_by_rule(self):
        (self.scratch / "target").write_bytes(b"t")
        os.symlink("target", self.scratch / "link")
        os.link(self.scratch / "target", self.scratch / "second")
        (self.scratch / "d").mkdir()
        os.symlink("d", self.scratch / "dlink")
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_read.read_file("link", self.root_fd, 10)
        self.assertEqual(refused.exception.rule_id, "file.symlink")
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_read.read_file("target", self.root_fd, 10)
        self.assertEqual(refused.exception.rule_id, "file.hardlink")
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_read.read_file("d", self.root_fd, 10)
        self.assertEqual(refused.exception.rule_id, "file.special")
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_read.open_directory("dlink", self.root_fd)
        self.assertEqual(refused.exception.rule_id, "file.symlink")
        with self.assertRaises(NotADirectoryError):
            safe_read.open_directory("target", self.root_fd)
        with self.assertRaises(FileNotFoundError):
            safe_read.read_file("absent", self.root_fd, 10)

    def test_a_path_under_the_root_is_opened_component_by_component(self):
        (self.scratch / "a" / "b").mkdir(parents=True)
        (self.scratch / "a" / "b" / "f").write_text("v\n", encoding="utf-8")
        os.symlink("a", self.scratch / "la")
        self.assertEqual(safe_read.read_text_under(self.root_fd, pathlib.PurePath("a/b/f"), 10), "v\n")
        with self.assertRaises(safe_read.RefusedRead):
            safe_read.read_text_under(self.root_fd, pathlib.PurePath("la/b/f"), 10)
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_read.read_text_under(self.root_fd, pathlib.PurePath("a/b/f"), 1)
        self.assertEqual(refused.exception.rule_id, "file.size")
        for bad in ("/a/b/f", "a/../b", ".", ""):
            with self.assertRaises(ValueError):
                safe_read.read_file_under(self.root_fd, pathlib.PurePath(bad), 10)
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_read.decode_text(b"\xff\xfe")
        self.assertEqual(refused.exception.rule_id, "file.binary")


class WalkReadsEachFileOnce(unittest.TestCase):
    """The validator reads a file once, from the walk, and no rule reopens it; counted by wrapping the reader."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.set_directory = self.scratch / "acme"
        shutil.copytree(FIXTURES / "pass" / "acme" / "acme", self.set_directory)
        self.options = set_validation.ValidationOptions(publisher="acme", profile="source", reserved_words=set(),
                                                        license_allowlist=fmt.DEFAULT_LICENSE_ALLOWLIST,
                                                        license_text_directories=[FIXTURES], display_root=self.scratch)

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def validate_counting_reads(self):
        reads = []

        def counting_read_file(name, dir_fd, max_bytes):
            reads.append(name)
            return safe_read.read_file(name, dir_fd, max_bytes)

        collector = FindingCollector("validate")
        with mock.patch.object(set_validation, "read_file", counting_read_file):
            set_validation.validate_set_directory(self.set_directory, self.options, collector)
        return reads, collector

    def test_the_passing_set_is_read_once_per_file(self):
        files = [path for path in self.set_directory.rglob("*") if path.is_file()]
        reads, collector = self.validate_counting_reads()
        self.assertEqual(collector.findings, [])
        self.assertEqual(sorted(reads), sorted(path.name for path in files))

    def test_an_over_size_config_file_is_read_once_and_refused_on_size_alone(self):
        upstream = self.set_directory / "skills" / "pdftext" / "UPSTREAM.conf"
        upstream.write_text(upstream.read_text(encoding="utf-8") + "# " + "x" * (1024 * 1024) + "\n", encoding="utf-8")
        reads, collector = self.validate_counting_reads()
        self.assertEqual(reads.count("UPSTREAM.conf"), 2, "the set's two UPSTREAM.conf, each once")
        self.assertEqual({finding.rule_id for finding in collector.findings}, {"file.size"})
        self.assertEqual(len(collector.findings), 1)

    def test_a_tripped_file_budget_stops_the_reads(self):
        references = self.set_directory / "skills" / "acme-pdf-processing" / "references"
        for directory in range(3):
            (references / f"part-{directory}").mkdir()
            for index in range(800):
                (references / f"part-{directory}" / f"page-{index}.md").write_text("# p\n", encoding="utf-8")
        reads, collector = self.validate_counting_reads()
        self.assertEqual(len(reads), fmt.SET_MAX_FILES, "the file after the budget is not opened")
        self.assertEqual([finding.rule_id for finding in collector.findings], ["file.size"])


class FormatRegistry(unittest.TestCase):
    def test_names(self):
        for name in ("a", "pdf-processing", "ai-tools-x1", "a" * 64):
            self.assertTrue(fmt.is_valid_name(name), name)
        for name in ("", "-a", "a-", "a--b", "A", "a_b", "a" * 65):
            self.assertFalse(fmt.is_valid_name(name), name)

    def test_prefixes(self):
        self.assertEqual(fmt.authored_asset_prefix("core"), "ai-tools-")
        self.assertEqual(fmt.authored_asset_prefix("ai-tools"), "ai-tools-")
        self.assertEqual(fmt.authored_asset_prefix("acme-dotnet"), "acme-dotnet-")

    def test_every_warning_rule_is_a_rule(self):
        self.assertTrue(fmt.RULES_WARNING <= set(fmt.RULES))

    def test_implemented_kinds(self):
        self.assertEqual([kind.kind_id for kind in fmt.IMPLEMENTED_KINDS], ["skills", "subagents"])
        self.assertFalse(fmt.KINDS_BY_ID["orientation"].is_set_admissible)


if __name__ == "__main__":
    unittest.main()
