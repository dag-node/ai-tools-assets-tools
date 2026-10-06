# Changelog

Changes to the tooling, released as tags `v<version>`; `v1` follows the latest
release of the major. The format follows [Keep
a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The asset format, format 1, stated in `format/FORMAT.md` with a rule id
  per rule, and `tools/validate` applying it to a publisher repository
  or to one set directory, with a release profile for a built set. The format
  states its `KEY=value` grammar (an empty list, an absent key and `key=[]`
  told apart; a publisher's own keys as `x_<name>`, every other unknown key
  refused), the type of every frontmatter field (a scalar YAML would read as
  a number, a boolean or null is quoted), and the bounds a set is read under
  (files, directories, depth, bytes, entries per directory, SPDX expression
  size, manifest size), at which validation stops with one finding.
- Every command opens a repository's files by descriptor without following
  a symbolic link and reads each once, so a linked set, manifest or `sets/`
  directory is refused before its target is read; `build-set` stages the
  bytes the validator read and replaces only a build of the same set it made;
  `link-set` removes an entry only while it still matches its record.
- The commands a publisher repository runs: `new-set`, `new-asset`,
  `sync-manifests`, `build-set` and `link-set`, beside `check-publisher`.
- The SPDX allowlist: every licence a set declares is on the list in force,
  the GPLv3-compatible permissive default or `publisher.conf` `licenses=`,
  and `check-licenses` holds every file of a repository to it, judging each
  declaration REUSE 3.2 applies to the file under its precedence and glob
  grammar.
- `check-signoff`, the Developer Certificate of Origin check over a pull
  request's own commits.
- The conformance fixtures under `fixtures/`: a passing set per publisher shape
  and one failing set per rule, each naming its rule, for a consumer
  that implements the same rules.
- The reusable `validate` workflow a publisher repository calls, running
  the validator, the manifest check, the licence check, `reuse lint`,
  `shellcheck` and the sign-off check (reported, not yet enforced) from
  a checkout of the repository and commit that define the workflow file (a fork
  names itself in `tools-repository`; GitHub Enterprise Server runs the tools
  from a local pin), and this repository's release workflow, which verifies one
  captured signed tag object and publishes the signed archive a consumer pins.
- The formatters from ai-tools-base under `formatters/`, `AGPL-3.0-only`,
  taking the checker as `--checker <path>`.

### Changed

- `check-publisher` reads the repository from `--root` (the current directory
  by default) and the reserved words beside itself, since it no longer lives
  in the repository it checks.
- `publisher.conf` carries `description=`, the marketplace's description,
  which `sync-manifests` renders.
