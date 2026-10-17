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
| `validate` | applies the format to every set, runs `check-publisher`, holds the committed manifests and the marketplace to `sync-manifests`, every declared licence to the list in force and every licence text to the text SPDX publishes; `--set-directory DIR` validates one set (with `--publisher-conf PATH`, its manifests are compared whole against that file's rendering and its `allow_dynamic_injection` applies; the repository root's is the default where it exists), `--profile release` a built one; `--list-rules` prints the rules |
| `check-publisher` | checks that the marketplace, the set names and the manifests' names and contacts are copied from `publisher.conf`; with `--repository <owner>/<repo>`, that `publisher.conf` describes that repository, that no identity key holds the template's placeholder `change-me` and that `REUSE.toml` does not name the template's package (every release tag) |
| `sync-manifests` | writes each set's `plugin.json` and `.claude-plugin/plugin.json` and the repository's `.claude-plugin/marketplace.json` from `set.conf` and `publisher.conf`; `--check` refuses a committed copy that differs |
| `new-publisher` | turns an unmodified copy of the publisher template ([dag-node/ai-tools-assets-template](https://github.com/dag-node/ai-tools-assets-template)) into the publisher's repository: `--repository <owner>/<repo> --contact <email>` writes `publisher.conf`, `LICENSE`, `REUSE.toml`, `CODEOWNERS`, `README.md` and the workflows' job key and signers, and replaces the example set with the publisher's first set; `--source <url>` names where the sets come from for a publisher that does not publish from GitHub; every check runs before the first write, a second run is refused, and the git steps are printed, not run |
| `new-set` | creates `sets/<set>/` with `set.conf`, `CHANGELOG.md` and `README.md`, and writes its manifests; refuses a name the format refuses |
| `new-asset` | creates a skill (`skills/<name>/SKILL.md`) or a subagent (`agents/<name>.md`) in a set, with the set's prefix |
| `build-set` | validates a set, stages it under `build/<set>/` with its licence texts and notices, writes `SHA256SUMS`, validates the stage under the release profile and zips it to `dist/ai-tools-assets-sets-<set>-<version>.zip` with its `.sha256` |
| `sign-set` | signs the `SHA256SUMS` of a set `build-set` staged with the publisher's own `gpg` key (`--key <fingerprint>`, or gpg's default), the agent asking for the passphrase, verifies the signature with `gpgv`, writes `SHA256SUMS.asc` beside it and prints the primary a host's binding names; a stale stage is refused before `gpg` runs |
| `link-set` | validates a set, then links (or with `--copy` copies) its skills into the skills directory an agent reads, records what it placed, and removes it again with `--remove`; a set that does not validate places nothing |
| `check-licenses` | holds every licence REUSE 3.2 applies to a tracked file (every SPDX header in the file or its `.license` sidecar, combined with the matching `REUSE.toml` annotation under its precedence) to the list in force, and refuses a `REUSE.toml` outside the subset it reads without judging any file on it; `--exception GLOB=ID` accepts one identifier for the files a REUSE glob matches (`dir/**`); warns for a text under `LICENSES/` that is not the text SPDX publishes, or whose digest neither the tools nor `publisher.conf` `license_text_digests` hold; the summary names each licence's class |
| `check-signoff` | requires an author-matching `Signed-off-by` trailer on every commit of `--range BASE..HEAD`, a pull request's own commits |

The reusable workflow `.github/workflows/validate.yml` runs these commands
from a checkout of the repository and commit that define the workflow file
(`job.workflow_repository` and `job.workflow_sha`), refuses a repository other
than `dag-node/ai-tools-assets-tools` unless the caller names its fork
in the `tools-repository` input, and refuses a file path other than its own.
A publisher calling a fork names it:

```yaml
jobs:
  validate:
    uses: acme/ai-tools-assets-tools/.github/workflows/validate.yml@v1
    with:
      tools-repository: acme/ai-tools-assets-tools
```

GitHub Enterprise Server does not define those fields, so a publisher on GHES
runs the commands from a local pin of this repository instead of calling
the workflow.

`reserved-words.txt` holds the words a set name does not start with; every
command reads it beside itself. `lib/` holds the modules the commands share:
the format registry, the descriptor-based file reader every command opens
a repository's files through and the writer that creates them without following
a link, the `KEY=value` reader, the frontmatter reader, the SPDX evaluator,
the licence table (each widely used licence's class and the digests of its published text), the manifest renderer,
the set validator, the portable name the `file.name`
finding proposes and the reader of an entry file's relative links.

```bash
python3 tools/new-set acme-dotnet --summary "ASP.NET Core skills"
python3 tools/new-asset acme-dotnet skill acme-dotnet-ef-migrations
python3 tools/build-set acme-dotnet
python3 tools/link-set acme-dotnet --target ~/.claude/skills
```
