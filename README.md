# ai-tools-assets-tools

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**The asset format for `ai-tools-assets` sets, the tools that check and build
a set, and the reusable workflows a publisher's CI calls.**

A set is a collection of skills and subagents a publisher keeps
under `sets/<set>/` in a repository such
as [ai-tools-assets](https://github.com/dag-node/ai-tools-assets), installable
as a Claude Code or Codex plugin as committed, and packaged for [Agent Tools
Restricted](https://github.com/dag-node/tools-agent-tools-restricted)
(`ai-tools-base`), which links an asset only when its root-owned enable list
names it. This repository holds what every such repository shares: the format
a set follows, the validator that applies it, the scaffolding and build
commands, and the GitHub workflows that run them.

**Status.** Format 1, the commands, the fixtures, the workflows and the
signed set release are in the tree and unreleased: the first tags are `v1.0.0`
and `v1`. Until then a publisher calls the workflows at a commit, and runs
the tools from a checkout.

## Layout

```text
format/         FORMAT.md: format 1, the kind table and the rule ids
tools/          validate, new-set, new-asset, sync-manifests, build-set,
                link-set, check-licenses, check-signoff, check-publisher,
                reserved-words.txt, lib/
fixtures/       conformance fixtures: sets that pass, and sets that fail
                one named rule each
formatters/     reflow prose at the column a checker names (AGPL-3.0-only)
packaging/      the release steps, the nFPM template of a set's RPM and the
                RPM signer
tests/          tests for tools/, packaging/ and the fixtures
.github/        validate.yml and set-release.yml, which a publisher's
                repository calls, and release.yml, this repository's own
                release
```

## Using the tools

A publisher repository calls the workflow from its own, pinned by the `v1`
tag or by a full commit id:

```yaml
jobs:
  validate:
    uses: dag-node/ai-tools-assets-tools/.github/workflows/validate.yml@v1
```

The same checks run from a checkout, against the repository of the current
directory:

```bash
python3 ../ai-tools-assets-tools/tools/validate
```

`tools/README.md` describes each command, and `packaging/README.md` the set
release a publisher's release workflow calls. A release is tagged `v<semver>`;
`v1` moves within the major, and a change a set would have to follow is a new
major and a new `format`.

## Formatters

The formatters under `formatters/` reflow a repository's prose at the column
the `prose-check.py` checker of the `ai-tools-technical-docs` skill names,
and take that checker as `--checker <path>`:

```bash
bash ../ai-tools-assets-tools/formatters/format.sh --checker <prose-check.py>
```

They are `AGPL-3.0-only`, unlike the rest of this repository, and no set
or template copies them.

## Community

- [Contributing](CONTRIBUTING.md), including the sign-off every commit carries
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

## License

MIT, see [LICENSE](LICENSE), for every file that does not state another;
`formatters/**` and `packaging/sign-rpms.sh` are `AGPL-3.0-only`, stated
in each file's header.
[REUSE.toml](REUSE.toml) covers files that do not state one, and `LICENSES/`
holds every text.
