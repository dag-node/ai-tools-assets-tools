# SPDX-License-Identifier: MIT
"""Holds the rule registry, the specification and the fixtures to one another.

format/FORMAT.md embeds the table `tools/validate --list-rules --markdown` prints, between two marker comments, so a
rule added to the registry is documented or the suite fails; every committed failing or warning fixture names a rule
the registry has; and every rule the registry has is covered by a fixture, by a case in tests/test_fixtures.py (the
trees git does not carry) or by a case in tests/test_commands.py (the repository-level rules), the three sets named
here so a new rule lands with its proof.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
sys.path.insert(0, str(REPOSITORY / "tools" / "lib"))
sys.path.insert(0, str(TESTS))

import asset_format as fmt  # noqa: E402
from fixture_generator import FAIL_FIXTURES, NAMED_SET_FIXTURES, PUBLISHER_CONF_FIXTURES, RELEASE_FIXTURES, VARIANT_FIXTURES  # noqa: E402

BEGIN_MARKER = "<!-- rules:begin -->"
END_MARKER = "<!-- rules:end -->"
# Rules whose fixture git does not carry; tests/test_fixtures.py builds each in a temporary directory.
UNCOMMITTABLE_RULES = {"file.hardlink", "file.special", "file.size", "file.name"}
# Rules about a repository rather than a set; tests/test_commands.py drives each.
REPOSITORY_RULES = {"repo.layout", "repo.publisher-conf", "repo.marketplace", "license.file"}


def load_validate_module():
    """Import tools/validate, a script without the .py suffix, as a module."""
    loader = importlib.machinery.SourceFileLoader("validate_command", str(REPOSITORY / "tools" / "validate"))
    spec = importlib.util.spec_from_loader("validate_command", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RulesDocumented(unittest.TestCase):
    def test_format_md_embeds_the_rule_table(self):
        text = (REPOSITORY / "format" / "FORMAT.md").read_text(encoding="utf-8")
        self.assertIn(BEGIN_MARKER, text)
        self.assertIn(END_MARKER, text)
        embedded = text.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0].strip("\n")
        expected = load_validate_module().rules_listing(markdown=True).strip("\n")
        self.assertEqual(embedded, expected, "run `python3 tools/validate --list-rules --markdown` and paste the table between the markers")

    def test_every_fixture_rule_is_a_rule(self):
        self.assertTrue(fixture_rules() <= set(fmt.RULES), fixture_rules() - set(fmt.RULES))
        self.assertTrue({rule for _, rule, _ in VARIANT_FIXTURES} <= {rule for rule, _, _ in FAIL_FIXTURES},
                        "a variant fixture follows its rule's first fixture")

    def test_every_rule_has_a_proof(self):
        uncovered = set(fmt.RULES) - fixture_rules() - UNCOMMITTABLE_RULES - REPOSITORY_RULES
        self.assertEqual(uncovered, set(), f"rules without a fixture or a named test: {sorted(uncovered)}")
        self.assertEqual(fixture_rules() & (UNCOMMITTABLE_RULES | REPOSITORY_RULES), set())


def fixture_rules():
    """Every rule a committed fixture names."""
    return ({rule for rule, _, _ in FAIL_FIXTURES} | {rule for _, rule, _ in RELEASE_FIXTURES}
            | {rule for _, rule, _, _ in NAMED_SET_FIXTURES} | {rule for _, rule, _ in VARIANT_FIXTURES}
            | {rule for _, rule, _ in PUBLISHER_CONF_FIXTURES})


if __name__ == "__main__":
    unittest.main()
