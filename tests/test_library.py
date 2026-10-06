# SPDX-License-Identifier: MIT
"""Unit tests for the readers under tools/lib: the KEY=value grammar, the frontmatter subset and SPDX expressions."""
from __future__ import annotations

import pathlib
import sys
import unittest

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "tools" / "lib"))

import asset_format as fmt  # noqa: E402
from frontmatter import split_frontmatter  # noqa: E402
from key_value_config import parse_key_value_text, parse_list_value  # noqa: E402
from spdx_expression import evaluate_expression  # noqa: E402


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
