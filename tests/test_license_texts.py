# SPDX-License-Identifier: MIT
"""The licence table in tools/lib/license_texts.py, read as the generator `packaging/license-text-digests.py` prints
it: every row's class is one of the table's, every digest is a SHA-256, the identifiers are unique and in the
generator's order, and the table does not hold any identifier SPDX deprecated."""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
TABLE_SOURCE = REPOSITORY / "tools" / "lib" / "license_texts.py"
sys.path.insert(0, str(REPOSITORY / "tools" / "lib"))

import license_texts  # noqa: E402
from license_texts import LICENSE_TEXTS  # noqa: E402

CLASSES = {license_texts.PUBLIC_DOMAIN, license_texts.PERMISSIVE, license_texts.WEAK_COPYLEFT, license_texts.COPYLEFT,
           license_texts.LIMITED_USE}
DIGEST = re.compile(r"^[0-9a-f]{64}$")
# Deprecated SPDX identifiers that Fedora and EPEL packages still carry; each has a current identifier the table holds
# or may hold instead.
DEPRECATED_IDENTIFIERS = ("BSD-2-Clause-FreeBSD", "GPL-2.0-with-classpath-exception", "LGPL-2.1", "GPL-2.0")


def table_identifiers_as_written():
    """The keys of the `LICENSE_TEXTS` literal in source order, duplicates kept, which the built dict drops."""
    module = ast.parse(TABLE_SOURCE.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "LICENSE_TEXTS":
            return [key.value for key in node.value.keys]
    raise AssertionError(f"{TABLE_SOURCE} does not assign LICENSE_TEXTS a dict literal")


class LicenseTableTest(unittest.TestCase):
    def test_every_class_is_one_of_the_tables(self):
        for identifier, entry in LICENSE_TEXTS.items():
            with self.subTest(identifier=identifier):
                self.assertIn(entry.license_class, CLASSES)

    def test_every_row_has_a_digest_and_each_is_a_sha256(self):
        for identifier, entry in LICENSE_TEXTS.items():
            with self.subTest(identifier=identifier):
                self.assertTrue(entry.digests, "a row joins with its digests filled by the generator")
                for digest in entry.digests:
                    self.assertRegex(digest, DIGEST)
                self.assertEqual(len(entry.digests), len(set(entry.digests)))

    def test_identifiers_are_unique_and_in_the_generators_order(self):
        identifiers = table_identifiers_as_written()
        self.assertEqual(len(identifiers), len(LICENSE_TEXTS))
        self.assertEqual(identifiers, sorted(identifiers, key=str.lower))
        self.assertEqual(len(identifiers), len({identifier.lower() for identifier in identifiers}))

    def test_no_deprecated_identifier(self):
        for identifier in DEPRECATED_IDENTIFIERS:
            with self.subTest(identifier=identifier):
                self.assertFalse(identifier in LICENSE_TEXTS, f"{identifier} is deprecated on the SPDX list")


if __name__ == "__main__":
    unittest.main()
