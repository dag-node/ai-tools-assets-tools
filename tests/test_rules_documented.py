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
from fixture_generator import (DYNAMIC_INJECTION_FIXTURES, FAIL_FIXTURES, NAMED_SET_FIXTURES,  # noqa: E402
                               PUBLISHER_CONF_FIXTURES, RELEASE_FIXTURES, VARIANT_FIXTURES)

BEGIN_MARKER = "<!-- rules:begin -->"
END_MARKER = "<!-- rules:end -->"
# Rules whose fixture git does not carry; tests/test_fixtures.py builds each in a temporary directory.
UNCOMMITTABLE_RULES = {"file.hardlink", "file.special", "file.size"}
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

    def test_a_rule_proven_by_a_test_is_named_in_that_test(self):
        """The exception sets are not taken on trust: the test file each names asserts on the rule id."""
        fixtures_tests = (TESTS / "test_fixtures.py").read_text(encoding="utf-8")
        commands_tests = (TESTS / "test_commands.py").read_text(encoding="utf-8")
        for rule in sorted(UNCOMMITTABLE_RULES):
            self.assertIn(rule, fixtures_tests, f"{rule} has no assertion in tests/test_fixtures.py")
        for rule in sorted(REPOSITORY_RULES):
            self.assertIn(rule, commands_tests, f"{rule} has no assertion in tests/test_commands.py")

    def test_a_variant_fixture_is_named_for_its_rule(self):
        failing = [(name, rule) for expect, name, rule, _, _ in DYNAMIC_INJECTION_FIXTURES if expect == "fail"]
        for name, rule in [(name, rule) for name, rule, _ in VARIANT_FIXTURES + PUBLISHER_CONF_FIXTURES + RELEASE_FIXTURES] + failing:
            self.assertTrue(name == rule or name.startswith(rule + "."), f"{name} is not <rule> or <rule>.<variant> for {rule}")


def fixture_rules():
    """Every rule a committed fixture names."""
    return ({rule for rule, _, _ in FAIL_FIXTURES} | {rule for _, rule, _ in RELEASE_FIXTURES}
            | {rule for _, rule, _, _ in NAMED_SET_FIXTURES} | {rule for _, rule, _ in VARIANT_FIXTURES}
            | {rule for _, rule, _ in PUBLISHER_CONF_FIXTURES} | {rule for _, _, rule, _, _ in DYNAMIC_INJECTION_FIXTURES if rule})


if __name__ == "__main__":
    unittest.main()
