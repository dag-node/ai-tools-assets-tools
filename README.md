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

**Status.** The repository holds `check-publisher` and the formatters.
The commands, the format specification, the fixtures and the workflows are
being added; each lands with its tests.

## Layout

```text
format/         FORMAT.md: format 1, the kind table and the rule ids
tools/          validate, new-set, new-asset, sync-manifests, build-set,
                link-set, check-publisher, reserved-words.txt, lib/
fixtures/       conformance fixtures: sets that pass, and sets that fail
                one named rule each
formatters/     reflow prose at the column a checker names (AGPL-3.0-only)
tests/          tests for tools/ and the fixtures
.github/        validate.yml and release.yml, called from a publisher's
                repository at a pinned tag
```

## Using the tools

A publisher repository pins a release of this repository in `tools.pin`
and calls the workflows from its own:

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

`tools/README.md` describes each command. A release is tagged `v<semver>`; `v1`
moves within the major, and a change a set would have to follow is a new major
and a new `format`.

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
`formatters/**` is `AGPL-3.0-only`, stated in each file's header.
[REUSE.toml](REUSE.toml) covers files that do not state one, and `LICENSES/`
holds every text.
