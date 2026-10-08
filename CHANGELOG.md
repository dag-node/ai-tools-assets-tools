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
  told apart; a publisher's own keys as `x_<key>`, every other unknown key
  refused), the type of every frontmatter field (a scalar YAML would read as
  a number, a boolean, null, a date or no value is quoted; one lexical rule
  holds every plain scalar wherever it stands), the content rules over the
  whole entry file, frontmatter included, and the bounds a set is read under
  (files, directories, depth, bytes, entries per directory, SPDX expression
  size, manifest size), at which validation stops with one finding. The
  format reserves `jobs/`, `libs/`, `variants/` and `llms.txt` at the set
  root and states how a reserved entry is added: additively, by a
  capability token a set carrying it must declare, so a reader without the
  token refuses the set whole; `format=2` is kept for a change in what an
  existing entry means.
- Every command opens a repository's files by descriptor without following
  a symbolic link and reads each once, so a linked set, manifest or `sets/`
  directory is refused before its target is read; a generated file is
  replaced as a directory entry, never written into, so a hard link planted
  at its name is refused; `build-set` stages the bytes the validator read
  and replaces only a build of the same set it made; `link-set` validates
  the set before placing a skill and removes an entry only while it still
  matches its record.
- The commands a publisher repository runs: `new-set`, `new-asset`,
  `sync-manifests`, `build-set` and `link-set`, beside `check-publisher`.
- The SPDX allowlist: every licence a set declares is on the list in force,
  the GPLv3-compatible permissive default or `publisher.conf` `licenses=`,
  and `check-licenses` holds every file of a repository to it, judging each
  declaration REUSE 3.2 applies to the file -- every header in the file,
  outside a REUSE ignore block -- under its precedence and glob grammar,
  and refusing a `REUSE.toml` outside the TOML subset it reads rather than
  judging any file on it.
- A skill or subagent may run a command when it loads, such as injecting
  `git status` output, once its `asset.conf` declares `skills.dynamic.v1`
  and `publisher.conf` sets `allow_dynamic_injection=yes`; every other
  substitution, and a declaration the publisher does not allow, fails
  `body.dynamic-injection`.
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
  captured signed tag object, confirms the remote still names it before
  publishing, and publishes the signed archive a consumer pins and the signed
  RPM `ai-tools-assets-tools`, which installs the tools, the format and the
  fixtures under `/usr/share/ai-tools-assets-tools/`.
- The reusable `set-release` workflow a publisher's release calls for a tag
  `<set>/v<semver>`: it verifies the signed tag, builds the set, signs the zip
  and `SHA256SUMS` with the org package-signing key, ships the set as the
  signed RPM `ai-tools-assets-<set>`, attaches them to the GitHub release
  and, when asked, has `dag-node/rpm` publish the RPM. See
  `packaging/README.md`.
- The formatters from ai-tools-base under `formatters/`, `AGPL-3.0-only`,
  taking the checker as `--checker <path>`.

### Changed

- `check-publisher` reads the repository from `--root` (the current directory
  by default) and the reserved words beside itself, since it no longer lives
  in the repository it checks.
- `publisher.conf` carries `description=`, the marketplace's description,
  which `sync-manifests` renders.
