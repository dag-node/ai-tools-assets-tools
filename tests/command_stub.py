# SPDX-License-Identifier: MIT
"""Answers for an external command that `test_release_steps.py` replaces: skopeo, gh, curl, sleep.

The test exports one bash function per command, `skopeo() { python3 command_stub.py skopeo "$@"; }`, through the
environment, so `release-steps.sh` calls this file where it calls the command, and no stub has to be an executable on
a temporary directory that may be mounted noexec. Each call is appended to the JSON-lines log `STUB_LOG` names.

Its answer comes from the rules in the JSON file `STUB_ANSWERS` names, in order: the first rule whose `command` matches
and whose `arguments` are a prefix of the call's answers it with `stdout`, `stderr` and `status`. A rule carrying
`times` answers that many calls and is then passed over, so a lookup can fail a set number of times before a later rule
answers it. A call no rule matches exits 127, naming itself on stderr.
"""
from __future__ import annotations

import json
import os
import sys


def main(command: str, arguments: list) -> int:
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"command": command, "arguments": arguments}) + "\n")
    path = os.environ["STUB_ANSWERS"]
    with open(path, encoding="utf-8") as answers_file:
        rules = json.load(answers_file)
    for rule in rules:
        if rule["command"] != command or arguments[:len(rule["arguments"])] != rule["arguments"]:
            continue
        if rule.get("times") is not None:
            if rule["times"] <= 0:
                continue
            rule["times"] -= 1
            with open(path, "w", encoding="utf-8") as answers_file:
                json.dump(rules, answers_file)
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        return rule.get("status", 0)
    sys.stderr.write(f"command_stub: no answer for {command} {' '.join(arguments)}\n")
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
