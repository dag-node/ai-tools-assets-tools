# Tests

```bash
python3 -m unittest discover -s tests -v
```

Standard library `unittest`, Python 3.9 or later, no install step. Each file
holds one concern:

| File | Proves |
|---|---|
| `test_library.py` | the readers under `tools/lib/`: the `KEY=value` grammar, the frontmatter subset, SPDX expressions, the name rules |
| `test_fixtures.py` | `tools/validate` over every fixture under `fixtures/`, each to its `fixture.conf`; the trees git does not carry (a hard link, a special file, an over-size file), built in a temporary directory |
| `test_commands.py` | the commands over a publisher repository composed from the passing fixture: scaffolding, the stale-manifest check, the build's inventory and zip, `link-set`, `check-licenses`, `check-signoff` over a synthetic history |
| `test_packaging.py` | `packaging/render-nfpm.py` over a set `build-set` staged: the rendered package fields, the refusals, and a value that stays one quoted scalar |
| `test_rules_documented.py` | the rule registry, `format/FORMAT.md` and the fixtures agree: every rule is documented and has a fixture or a named test |
| `fixture_generator.py` | not a test: writes the fixtures from one base tree (`generate`), and `check` fails when a committed fixture differs |

A fixture is one set directory beside a `fixture.conf` naming the outcome
(`expect=pass|fail|warn`), the rule, the publisher, the profile
and where the licence texts are. A failing fixture fails on its rule alone,
so the suite reads a validator's output rule by rule, and a consumer
that implements the same rules runs its own validator over the same trees.
The three `file.binary.*` fixtures that carry a bidi control or a byte order
mark do so on purpose, as the input the rule refuses; GitHub shows its
hidden-Unicode warning on those files for that reason.
