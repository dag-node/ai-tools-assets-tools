# SPDX-License-Identifier: MIT
"""What a check reports: a finding names the path, the rule it applies, what was found, and whether it refuses or warns.

Every command prints one line per finding, `<command>: <path>: <rule-id>: <what>`, with `warning: ` after the command
name for a finding that does not refuse, so a test and a CI log grep one shape. A rule id is the stable identifier the
format specification states beside the rule; the message is for the person reading the line.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import List, TextIO

REFUSE = "refuse"
WARN = "warn"


@dataclass(frozen=True)
class Finding:
    """One thing a check found, at `path`, under `rule_id`."""

    path: str
    rule_id: str
    message: str
    severity: str = REFUSE


class FindingCollector:
    """Collects the findings of one command run and prints them in the order they were found."""

    def __init__(self, command_name: str) -> None:
        self.command_name = command_name
        self.findings: List[Finding] = []

    def refuse(self, path: str, rule_id: str, message: str) -> None:
        self.findings.append(Finding(str(path), rule_id, message, REFUSE))

    def warn(self, path: str, rule_id: str, message: str) -> None:
        self.findings.append(Finding(str(path), rule_id, message, WARN))

    def extend(self, other: "FindingCollector") -> None:
        self.findings.extend(other.findings)

    def refusals(self) -> List[Finding]:
        return [finding for finding in self.findings if finding.severity == REFUSE]

    def has_refusals(self) -> bool:
        return any(finding.severity == REFUSE for finding in self.findings)

    def report(self, stream: TextIO = sys.stderr) -> int:
        """Print every finding and return the exit status: 1 while a refusal was found, 0 otherwise."""
        for finding in self.findings:
            severity_prefix = "" if finding.severity == REFUSE else "warning: "
            print(f"{self.command_name}: {severity_prefix}{finding.path}: {finding.rule_id}: {finding.message}",
                  file=stream)
        return 1 if self.has_refusals() else 0
