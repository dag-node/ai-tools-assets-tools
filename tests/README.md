# Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Standard library `unittest`, Python 3.9 or later, no install step. The
variable keeps the bytecode cache out of the checkout; it reaches each command
a test runs, where `python3 -B` does not. Each file holds one concern:

| File | Proves |
|---|---|
| `test_library.py` | the readers and the writer under `tools/lib/`: the `KEY=value` grammar, the frontmatter subset, SPDX expressions, the name rules, a write a swapped directory does not redirect |
| `test_fixtures.py` | `tools/validate` over every fixture under `fixtures/`, each to its `fixture.conf`; the trees git does not carry (a hard link, a special file, an over-size file), built in a temporary directory |
| `test_commands.py` | the commands over a publisher repository composed from the passing fixture: scaffolding, the stale-manifest check, the build's inventory and zip, `link-set`, `check-licenses`, `check-signoff` over a synthetic history |
| `test_packaging.py` | `packaging/render-nfpm.py` over a set `build-set` staged: the rendered package fields, the refusals, and a value that stays one quoted scalar |
| `test_release_steps.py` | `packaging/release-steps.sh check-images` over a copy of the release steps: every image resolves, a digest the registry no longer serves, a registry it cannot read, an absent `skopeo`, a reference without a digest; run from a caller's checkout, the `uses:` lines held to one ref; `refresh-images` over a scratch checkout with a bare origin: the pin line, the changelog entry, the commit and the pull request it generates, its refusals, its retries, and a re-run after each step; `repin-tools` in a scratch caller: a refresh commit, a commit that is not one, the pins already at the target, a release tag signed by a key generated for the test (where `gpg` runs); the swap check and `swap-off` over `/proc/swaps` fixtures, and the release workflows read as text: `swap-off` in a step that does not receive a secret, before the first that does; `find-tests` over a fixture tree, a failed `find` included; the floor interpreter check against a `python3` that reports a version, and `test-floor`'s refusal of a directory outside the checkout |
| `test_formatters.py` | `formatters/verify-reflow.py` over a scratch repository: a pure reflow, a changed word, a base that does not exist, a path one copy does not hold, a column table realigned, broken or joined, every difference in a file and a respaced line; `formatters/align-tables.py` over a table beside a fence; where Emacs is installed, `formatters/fill-comments.sh` over each shape it fills by a rule of its own and a table of three columns it leaves |
| `test_rules_documented.py` | the rule registry, `format/FORMAT.md` and the fixtures agree: every rule is documented and has a fixture or a named test |
| `command_stub.py` | not a test: answers for `skopeo`, `gh`, `curl` and `sleep` in `test_release_steps.py`, which exports it to the script as a bash function and logs each call |
| `fixture_generator.py` | not a test: writes the fixtures from one base tree (`generate`), and `check` fails when a committed fixture differs |

A fixture is one set directory beside a `fixture.conf` naming the outcome
(`expect=pass|fail|warn`), the rule, the publisher, the profile
and where the licence texts are. A failing fixture fails on its rule alone,
so the suite reads a validator's output rule by rule, and a consumer
that implements the same rules runs its own validator over the same trees.
The three `file.binary.*` fixtures that carry a bidi control or a byte order
mark do so on purpose, as the input the rule refuses; GitHub shows its
hidden-Unicode warning on those files for that reason.
