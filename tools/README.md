# Tools

```bash
python3 ../ai-tools-assets-tools/tools/validate
```

Each command is Python 3.9 and the standard library, called through its
interpreter from the publisher repository it acts on (or with `--root DIR`).
A command prints one line per finding, `<command>: <path>: <rule-id>: <what>`,
and exits 1 when a finding refuses; the rule ids are
[format/FORMAT.md](../format/FORMAT.md).

| Command | What it does |
|---|---|
| `validate` | applies the format to every set, runs `check-publisher`, holds the committed manifests and the marketplace to `sync-manifests`, and every declared licence to the list in force; `--set-directory DIR` validates one set, `--profile release` a built one; `--list-rules` prints the rules |
| `check-publisher` | checks that the marketplace, the set names and the manifests' names and contacts are copied from `publisher.conf`; with `--repository <owner>/<repo>`, that `publisher.conf` describes that repository (every release tag) |
| `sync-manifests` | writes each set's `plugin.json` and `.claude-plugin/plugin.json` and the repository's `.claude-plugin/marketplace.json` from `set.conf` and `publisher.conf`; `--check` refuses a committed copy that differs |
| `new-set` | creates `sets/<set>/` with `set.conf`, `CHANGELOG.md` and `README.md`, and writes its manifests; refuses a name the format refuses |
| `new-asset` | creates a skill (`skills/<name>/SKILL.md`) or a subagent (`agents/<name>.md`) in a set, with the set's prefix |
| `build-set` | validates a set, stages it under `build/<set>/` with its licence texts and notices, writes `SHA256SUMS`, validates the stage under the release profile and zips it to `dist/ai-tools-assets-<set>-<version>.zip` with its `.sha256` |
| `link-set` | links (or with `--copy` copies) a set's skills into the skills directory an agent reads, records what it placed, and removes it again with `--remove` |
| `check-licenses` | holds every tracked file's SPDX header or `REUSE.toml` annotation to the list in force; `--exception GLOB=ID` admits one identifier for the files a glob matches |
| `check-signoff` | requires an author-matching `Signed-off-by` trailer on every commit of `--range BASE..HEAD`, a pull request's own commits |

`reserved-words.txt` holds the words a set name does not start with; every
command reads it beside itself. `lib/` holds the modules the commands share:
the format registry, the `KEY=value` reader, the frontmatter reader, the SPDX
evaluator, the manifest renderer and the set validator.

```bash
python3 tools/new-set acme-dotnet --summary "ASP.NET Core skills"
python3 tools/new-asset acme-dotnet skill acme-dotnet-ef-migrations
python3 tools/build-set acme-dotnet
python3 tools/link-set acme-dotnet --target ~/.claude/skills
```
