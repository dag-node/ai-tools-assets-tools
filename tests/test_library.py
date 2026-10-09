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
import safe_write  # noqa: E402
import set_validation  # noqa: E402
from findings import FindingCollector  # noqa: E402
from frontmatter import Scalar, split_frontmatter  # noqa: E402
from key_value_config import parse_key_value_text, parse_list_value  # noqa: E402
from markdown_links import iter_relative_link_targets  # noqa: E402
from portable_name import propose_portable_name  # noqa: E402
from spdx_expression import evaluate_expression  # noqa: E402

FIXTURES = REPOSITORY / "fixtures"


class KeyValueGrammar(unittest.TestCase):
    def test_reads_the_grammar_base_reads(self):
        document = parse_key_value_text('# c\nformat=1\nname = core\nsummary="Community baseline" # x\nempty=\nq="[a]"\n')
        self.assertEqual(document.values, {"format": "1", "name": "core", "summary": "Community baseline", "empty": "", "q": "[a]"})
        self.assertTrue(document.has("empty"))
        self.assertEqual(document.get_list("q"), ((), "a bracketed list is written without quotes around it"))
        self.assertEqual(document.syntax_errors(), [])

    def test_reports_a_line_without_equals_and_a_repeated_key(self):
        document = parse_key_value_text("a=1\nbad line\na=2\n")
        self.assertEqual(document.get("a"), "2")
        self.assertEqual(len(document.syntax_errors()), 2)

    def test_reports_a_quote_that_does_not_close_and_text_after_one(self):
        document = parse_key_value_text('a="MIT\nb="MIT"garbage\nc="MIT" # fine\nd=\'x\' \n')
        self.assertEqual(document.values, {"a": "MIT", "b": "MIT", "c": "MIT", "d": "x"})
        errors = document.syntax_errors()
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("line 1: `a=`: the \" quote does not close", errors[0])
        self.assertIn("line 2: `b=`: `garbage` follows the closing quote", errors[1])

    def test_lists(self):
        self.assertEqual(parse_list_value("[a, b]"), (("a", "b"), None))
        self.assertEqual(parse_list_value("a, b c"), (("a", "b", "c"), None))
        self.assertEqual(parse_list_value("[]"), ((), None))
        self.assertEqual(parse_list_value("[ ]"), ((), None))
        self.assertEqual(parse_list_value("[a")[0], ())
        self.assertEqual(parse_list_value("[a, 'b']")[0], ())
        self.assertEqual(parse_list_value("[a,,b]"), ((), "an item between two commas is empty"))
        self.assertEqual(parse_list_value("[a, b,]")[0], ())
        self.assertEqual(parse_list_value(""), ((), "it is empty; write `key=[]` for an explicit empty list, or omit the key"))


class FrontmatterSubset(unittest.TestCase):
    def test_reads_scalars_maps_and_lists(self):
        document, body = split_frontmatter('---\nname: pdf\ndescription: "Extract text. Use when: x."\nmetadata:\n  ai-tools-libs: python\ntools: [Read, Grep]\nskills:\n  - a\nmaxTurns: 3\n---\n# Body\n')
        self.assertEqual(document.errors, [])
        self.assertEqual(document.values["description"], Scalar("Extract text. Use when: x.", quoted=True))
        self.assertEqual(document.values["metadata"], {"ai-tools-libs": Scalar("python")})
        self.assertEqual(document.values["tools"], [Scalar("Read"), Scalar("Grep")])
        self.assertEqual(document.values["skills"], [Scalar("a")])
        self.assertEqual(document.values["maxTurns"], Scalar("3"))
        self.assertEqual(body, "# Body\n")

    def test_refuses_what_is_outside_the_subset(self):
        document, _ = split_frontmatter("---\nname: x\nname: y\ndesc: |\n  block\n\tbad: tab\nnested:\n  a:\n    b: c\n")
        joined = "\n".join(document.errors)
        for fragment in ("given again", "opening with `|`", "tab in the indentation", "indentation changes", "does not close"):
            self.assertIn(fragment, joined)

    def test_refuses_the_ambiguous_scalars_other_readers_type_differently(self):
        cases = {
            "description: a: b\n": "does not carry `: `",
            "description: a #b\n": "does not carry ` #`",
            'description: "a\\qb"\n': "is not an escape this subset reads",
            "description: 'unclosed\n": "does not close",
            'description: "a" b\n': "text follows the closing quote",
            "tools: [*alias, Grep]\n": "opening with `*`",
            "tools: [a,,b]\n": "empty item",
            "tools: [a: b]\n": "does not carry `: `",
            "description: hello:\tworld\n": "does not carry `: `",
            "tools: [#comment]\n": "opening with `#`",
            "tools: [@bad]\n": "opening with `@`",
            "tools: [a\t#comment]\n": "does not carry ` #`",
            "tools: [a'b]\n": "carries a `,`, a bracket, a brace or a quote",
            "tools:\n  - - nested\n": "opening with `-`",
            "metadata:\n  k: - x\n": "opening with `-`",
        }
        for line, fragment in cases.items():
            document, _ = split_frontmatter(f"---\nname: x\n{line}---\n")
            self.assertTrue(any(fragment in error for error in document.errors), (line, document.errors))

    def test_quoting_resolves_escapes_and_marks_the_scalar(self):
        document, _ = split_frontmatter('---\na: "say \\"hi\\" \\\\ now"\nb: \'it\'\'s\'\nc: plain\nd: true\ne: "true"\n---\n')
        self.assertEqual(document.errors, [])
        self.assertEqual(document.values["a"], Scalar('say "hi" \\ now', quoted=True))
        self.assertEqual(document.values["b"], Scalar("it's", quoted=True))
        self.assertEqual(document.values["c"], Scalar("plain"))
        self.assertTrue(document.values["d"].is_yaml_typed)
        self.assertFalse(document.values["e"].is_yaml_typed)
        for text in ("true", "No", "ON", "null", "~", "12", "-3", "0x1F", "1_000", "1.5", "1e3", ".inf", ".NaN", "0o17",
                     "1:20", "-1:20:30", "1:20.5", "2026-10-06", "2001-12-14t21:59:43.10-05:00", "2001-12-14 21:59:43.10 -5", "2001-12-14T21:59:43Z"):
            self.assertTrue(Scalar(text).is_yaml_typed, text)
        for text in ("Read", "python3.12", "1.2.3", "v1", "none", "nope", "1-2", "a1", "10:x", "2026-10", "0:20"):
            self.assertFalse(Scalar(text).is_yaml_typed, text)

    def test_an_omitted_value_is_told_from_a_quoted_empty_string(self):
        document, _ = split_frontmatter('---\na:\nb: # comment\nc: ""\nmetadata:\n  k:\n  m: # c\n---\n')
        self.assertEqual(document.errors, [])
        self.assertTrue(document.values["a"].is_omitted)
        self.assertTrue(document.values["b"].is_omitted)
        self.assertFalse(document.values["c"].is_omitted)
        self.assertEqual(document.values["c"], Scalar("", quoted=True))
        self.assertTrue(all(scalar.is_omitted for scalar in document.values["metadata"].values()))

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

    def test_bounded_before_it_is_parsed(self):
        deep = "(" * 600 + "MIT" + ")" * 600
        self.assertIn("at most 64", evaluate_expression(deep, self.allowlist).syntax_error)
        nested = "(" * 9 + "MIT" + ")" * 9
        self.assertIn("more than 8 parentheses", evaluate_expression(nested, self.allowlist).syntax_error)
        self.assertTrue(evaluate_expression("(" * 8 + "MIT" + ")" * 8, self.allowlist).is_allowed)
        self.assertIn("at most 64", evaluate_expression(" AND ".join(["MIT"] * 40), self.allowlist).syntax_error)


class ReuseGlobs(unittest.TestCase):
    def test_star_and_question_mark_stop_at_a_slash_and_double_star_crosses_it(self):
        from license_policy import matches_reuse_glob
        cases = [
            ("src/*", "src/x.py", True), ("src/*", "src/vendor/x.py", False), ("src/**", "src/vendor/x.py", True),
            ("src/**", "src", False), ("src/**/x.py", "src/x.py", True), ("src/**/x.py", "src/a/b/x.py", True),
            ("**/x.py", "x.py", True), ("**/x.py", "a/x.py", True), ("**", "a/b/c", True), ("*.md", "a.md", True),
            ("*.md", "d/a.md", False), ("one/?.py", "one/a.py", True), ("one/?.py", "one/ab.py", False),
            ("one/?.py", "one//.py", False), ("lit\\*.txt", "lit*.txt", True), ("lit\\*.txt", "lita.txt", False),
            ("a.b", "aXb", False), ("[a].py", "[a].py", True), ("[a].py", "a.py", False),
        ]
        for glob, path, expected in cases:
            self.assertEqual(matches_reuse_glob(path, glob), expected, (glob, path))

    def test_the_match_is_bounded_by_the_lengths_and_refuses_what_it_does_not_read(self):
        import time
        from license_policy import matches_reuse_glob, reuse_glob_problem
        started = time.monotonic()
        self.assertFalse(matches_reuse_glob("a" * 40, "*a" * 28 + "b"))
        self.assertTrue(matches_reuse_glob("a" * 40, "*a" * 28))
        self.assertFalse(matches_reuse_glob("/".join(["a"] * 30), "/".join(["**", "*a"] * 10 + ["b"])))
        self.assertLess(time.monotonic() - started, 1.0, "the review's backtracking input returns at once")
        self.assertIsNone(reuse_glob_problem("**/x/**/y.py"))
        self.assertIn("at most 256", reuse_glob_problem("a" * 257))
        for glob in ("src**", "**.py", "a/***/b"):
            self.assertIn("stands alone", reuse_glob_problem(glob), glob)


class ControlCharacters(unittest.TestCase):
    def test_the_set_is_every_cc_but_tab_lf_cr_plus_bidi_controls_and_the_bom(self):
        import unicodedata
        expected = {code for code in range(0x110000) if unicodedata.category(chr(code)) == "Cc"} - {0x09, 0x0A, 0x0D}
        expected |= {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF}
        self.assertEqual(set(fmt.CONTROL_CODE_POINTS) | set(fmt.BIDI_CONTROL_CODE_POINTS) | {fmt.BYTE_ORDER_MARK}, expected)
        for code in sorted(expected):
            self.assertIsNotNone(fmt.CONTROL_CHARACTERS.search("a" + chr(code) + "b"), hex(code))
        for text in ("plain\ttab\r\nline", "caf" + chr(0xE9), chr(0x2014) + "dash", chr(0x200B) + "a zero-width space is not a bidi control"):
            self.assertIsNone(fmt.CONTROL_CHARACTERS.search(text), ascii(text))


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


class SafeWrite(unittest.TestCase):
    """The writer: a write lands in the directory it opened, whatever stands at that name after, and no link is written
    through."""

    def setUp(self):
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.root = self.scratch / "root"
        self.outside = self.scratch / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        (self.root / "output").mkdir()

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def swapping(self, opens):
        """`os.open` patched so that, on the open whose name `opens` accepts, `root/output` has been renamed away and a
        link to `outside` stands at its name: the swap between the inspection of the parent and the write into it."""
        original = os.open

        def swap(path, *arguments, **keywords):
            if opens(os.fspath(path)):
                (self.root / "output").rename(self.root / "previous-output")
                (self.root / "output").symlink_to(self.outside, target_is_directory=True)
            return original(path, *arguments, **keywords)
        return mock.patch.object(safe_write.os, "open", side_effect=swap)

    def test_a_parent_swapped_for_a_link_after_it_is_opened_does_not_redirect_the_write(self):
        with self.swapping(lambda name: name == "probe.txt"):
            safe_write.create_file(self.root, pathlib.PurePath("output/probe.txt"), b"created")
        self.assertEqual(list(self.outside.iterdir()), [], "nothing is created through the link")
        self.assertEqual((self.root / "previous-output" / "probe.txt").read_bytes(), b"created", "the write landed in the directory the inspection opened")
        (self.root / "output").unlink()
        (self.root / "previous-output").rename(self.root / "output")
        with self.swapping(lambda name: name.startswith(".probe.txt.")):
            safe_write.replace_file(self.root, pathlib.PurePath("output/probe.txt"), b"replaced")
        self.assertEqual(list(self.outside.iterdir()), [], "nothing is replaced through the link")
        self.assertEqual((self.root / "previous-output" / "probe.txt").read_bytes(), b"replaced")
        self.assertEqual([path.name for path in (self.root / "previous-output").iterdir()], ["probe.txt"], "no temporary name is left")

    def test_refuses_a_link_at_a_component_a_link_at_the_name_and_a_file_where_a_directory_goes(self):
        os.symlink(self.outside, self.root / "linked", target_is_directory=True)
        (self.root / "plain").write_bytes(b"not a directory\n")
        for relative, rule in (("linked/a.md", "file.symlink"), ("plain/a.md", "file.special")):
            for write in (safe_write.create_file, safe_write.replace_file):
                with self.assertRaises(safe_read.RefusedRead) as refused:
                    write(self.root, pathlib.PurePath(relative), b"x")
                self.assertEqual(refused.exception.rule_id, rule, relative)
        os.symlink(self.outside / "dangling.md", self.root / "output" / "dangling.md")
        for write in (safe_write.create_file, safe_write.replace_file):
            with self.assertRaises(safe_read.RefusedRead) as refused:
                write(self.root, pathlib.PurePath("output/dangling.md"), b"x")
            self.assertEqual(refused.exception.rule_id, "file.symlink")
        (self.outside / "other.md").write_bytes(b"other")
        os.link(self.outside / "other.md", self.root / "output" / "generated.md")
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_write.replace_file(self.root, pathlib.PurePath("output/generated.md"), b"x")
        self.assertEqual(refused.exception.rule_id, "file.hardlink")
        self.assertEqual(list(self.outside.iterdir()), [self.outside / "other.md"], "nothing is written through a link")
        self.assertEqual((self.outside / "other.md").read_bytes(), b"other", "the inode behind the other name is unchanged")
        self.assertEqual((self.root / "plain").read_bytes(), b"not a directory\n")

    def test_creates_the_missing_directories_and_replaces_an_entry_without_a_leftover(self):
        deep = pathlib.PurePath("output/new/deep/a.md")
        safe_write.create_file(self.root, deep, b"a")
        self.assertEqual((self.root / deep).read_bytes(), b"a")
        with self.assertRaises(safe_read.RefusedRead) as refused:
            safe_write.create_file(self.root, deep, b"again")
        self.assertEqual(refused.exception.rule_id, "file.symlink")
        safe_write.replace_file(self.root, deep, b"b")
        safe_write.replace_file(self.root, pathlib.PurePath("output/new/deep/b.md"), b"created by replace")
        self.assertEqual((self.root / deep).read_bytes(), b"b")
        self.assertEqual(sorted(path.name for path in (self.root / "output" / "new" / "deep").iterdir()), ["a.md", "b.md"])
        for bad in ("/a/b", "a/../b", ".", ""):
            with self.assertRaises(ValueError):
                safe_write.create_file(self.root, pathlib.PurePath(bad), b"x")


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

    def test_a_truncated_read_is_charged_against_the_aggregate_budget(self):
        references = self.set_directory / "skills" / "acme-pdf-processing" / "references"
        for index in range(70):
            (references / f"big-{index:02}.md").write_bytes(b"x" * (fmt.FILE_MAX_BYTES + 1))
        reads, collector = self.validate_counting_reads()
        self.assertLess(len([name for name in reads if name.startswith("big-")]), 70, "the files after the budget are not opened")
        self.assertEqual({finding.rule_id for finding in collector.findings}, {"file.size"})
        self.assertEqual([finding.path for finding in collector.findings if "more than" in finding.message], ["acme"],
                         "one root finding names the budget, beside the per-file findings made before it tripped")

    def test_a_tripped_file_budget_stops_the_reads(self):
        references = self.set_directory / "skills" / "acme-pdf-processing" / "references"
        for directory in range(3):
            (references / f"part-{directory}").mkdir()
            for index in range(800):
                (references / f"part-{directory}" / f"page-{index}.md").write_text("# p\n", encoding="utf-8")
        reads, collector = self.validate_counting_reads()
        self.assertEqual(len(reads), fmt.SET_MAX_FILES, "the file after the budget is not opened")
        self.assertEqual([finding.rule_id for finding in collector.findings], ["file.size"])


class MarkdownLinks(unittest.TestCase):
    def targets(self, text: str):
        return list(iter_relative_link_targets(text.split("\n")))

    def test_reads_inline_links_images_and_definitions(self):
        text = ("[a](references/a.md) ![b](assets/b.txt \"title\") [c](<scripts/c d.py>)\n"
                "[d]: ./references/d.md#part\n   [e]: <e.md>\n")
        self.assertEqual(self.targets(text), [(1, "references/a.md"), (1, "assets/b.txt"), (1, "scripts/c d.py"),
                                              (2, "./references/d.md"), (3, "e.md")])

    def test_reads_past_a_uri_an_anchor_and_code(self):
        text = ("[w](https://example.org/x.md) [m](mailto:a@b.example) [t](#top) `[s](span.md)` ``[s2](`x`.md)``\n"
                "````markdown\n[f](fenced.md)\n```\nstill [g](fenced.md)\n````\n~~~\n[h](tilde.md)\n~~~\n[k](after.md)\n")
        self.assertEqual(self.targets(text), [(10, "after.md")])

    def test_numbers_lines_from_the_first_line_given(self):
        self.assertEqual(list(iter_relative_link_targets(["", "[a](a.md)"], 7)), [(8, "a.md")])


class FormatRegistry(unittest.TestCase):
    def test_names(self):
        for name in ("a", "pdf-processing", "ai-tools-x1", "a" * 64):
            self.assertTrue(fmt.is_valid_name(name), name)
        for name in ("", "-a", "a-", "a--b", "A", "a_b", "a" * 65):
            self.assertFalse(fmt.is_valid_name(name), name)

    def test_portable_file_names(self):
        for name in ("SKILL.md", "a", ".hidden", "_x", "a-b.c_d", "x" * 255):
            self.assertTrue(fmt.is_portable_file_name(name), name)
        for name in ("", ".", "..", "-a", "two words", "a\\b", "r\u00e9sum\u00e9", "a\n", "a/b", "x" * 256):
            self.assertFalse(fmt.is_portable_file_name(name), repr(name))

    def test_portable_name_proposal(self):
        cases = {"two words.md": "two_words.md", "r\u00e9sum\u00e9.md": "resume.md", "a\\b.md": "a_b.md",
                 "-notes.md": "_notes.md", "a  \t b": "a_b", "\ufb01le.md": "file.md", "x" * 300: "x" * 255}
        for name, proposal in cases.items():
            self.assertEqual(propose_portable_name(name), proposal, repr(name))
        hashed = propose_portable_name("\u65e5\u672c")
        self.assertRegex(hashed, r"^renamed-[0-9a-f]{16}$")
        self.assertNotEqual(hashed, propose_portable_name("\u4e2d\u56fd"))
        for name in list(cases) + ["\u65e5\u672c", "..", "\udcff"]:
            self.assertTrue(fmt.is_portable_file_name(propose_portable_name(name)), repr(name))

    def test_prefixes(self):
        self.assertEqual(fmt.authored_asset_prefix("core"), "ai-tools-")
        self.assertEqual(fmt.authored_asset_prefix("ai-tools"), "ai-tools-")
        self.assertEqual(fmt.authored_asset_prefix("acme-dotnet"), "acme-dotnet-")

    def test_every_warning_rule_is_a_rule(self):
        self.assertTrue(fmt.RULES_WARNING <= set(fmt.RULES))

    def test_implemented_kinds(self):
        self.assertEqual([kind.kind_id for kind in fmt.IMPLEMENTED_KINDS], ["skills", "subagents"])
        self.assertFalse(fmt.KINDS_BY_ID["orientation"].is_allowed_in_set)


if __name__ == "__main__":
    unittest.main()
