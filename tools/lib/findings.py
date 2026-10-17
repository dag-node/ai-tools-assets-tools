# SPDX-License-Identifier: MIT
"""What a check reports: a finding names the path, the rule it applies, what was found, and whether it refuses or warns.

Every command prints one line per finding, `<command>: <path>: <rule-id>: <what>`, with `warning: ` after the command
name for a finding that does not refuse, so a test and a CI log grep one shape. A rule id is the stable identifier the
format specification states beside the rule; the message is for the person reading the line. Each command's `main` runs
its work through `refuse_unconverted_errors`, so an operating-system error that no reader turned into a finding is
printed in the same shape, under a rule the command already reports, and not as a traceback.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, TextIO

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

    def report(self, stream: Optional[TextIO] = None) -> int:
        """Print every finding to `stream`, standard error by default, and return the exit status: 1 while a refusal
        was found, 0 otherwise."""
        stream = stream if stream is not None else sys.stderr
        for finding in self.findings:
            severity_prefix = "" if finding.severity == REFUSE else "warning: "
            print(f"{self.command_name}: {severity_prefix}{finding.path}: {finding.rule_id}: {finding.message}",
                  file=stream)
        return 1 if self.has_refusals() else 0


def refuse_unconverted_errors(collector: FindingCollector, rule_id: str, default_path, body: Callable[[], int],
                              status: int = 1) -> int:
    """`body()`'s exit status. An OSError it lets through, one no reader turned into a finding, is reported instead of
    a traceback: a refusal under `rule_id` naming the path the failed call names (`default_path` where it names none),
    every finding printed, and `status` returned. SystemExit and KeyboardInterrupt are not OSError, so they pass."""
    try:
        return body()
    except OSError as error:
        path = error.filename if isinstance(error.filename, (str, bytes, os.PathLike)) else default_path
        collector.refuse(os.fsdecode(path), rule_id, f"{error.strerror or error}; {collector.command_name} stopped before it finished")
        collector.report()
        return status
