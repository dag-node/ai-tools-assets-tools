# Changelog

Changes to the tooling, released as tags `v<version>`; `v1` follows the latest
release of the major. The format follows [Keep
a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- The writer behind `new-set`, `new-asset`, `sync-manifests`, `build-set`
  and `link-set` opened the directory it wrote into by path after checking its
  components, so a component replaced with a symbolic link between the check
  and the write redirected the file to the link's target. The writer now opens
  each component from a directory descriptor without following a link,
  as the reader does, and creates, writes and renames at that descriptor:
  a swapped component leaves the write in the directory that was inspected.
- `check-licenses` read a file it could not inspect (a second hard link,
  a special file, a permission failure, an unreadable `.license` sidecar)
  as one with no header, and a matching `REUSE.toml` annotation then covered
  it. Such a file is now refused under `license.file`. A tracked symbolic link
  is not judged, as `reuse lint` does not judge one.
- `verify-reflow.py` reported a `--base` revision git could not resolve,
  or an `--against` directory that does not exist, as every path skipped
  and exited 0. The base is now verified before any path is read and such a run
  exits 1; a path the tree does not hold is a failure, and a skip is a path
  the tree holds and the base does not.
- `align-tables.py` raised a `TypeError` and stopped on a fenced block
  that immediately followed a comment table, leaving later files unprocessed.
  A fence now ends the table before it, and fenced lines are never part
  of a block.

## [1.0.0] - 2026-10-08

### Added

- Asset format 1 (`format/FORMAT.md`), with a rule id per rule.
  `tools/validate` applies it to a publisher repository or, with
  `--set-directory`, to one set; `--profile release` adds the inventory
  check a built set carries. The `KEY=value` grammar tells an empty list,
  an absent key and `key=[]` apart; a publisher's own keys are `x_<key>`,
  and every other unknown key is refused. A frontmatter scalar that YAML
  would read as
  a number, a boolean, null or a date is quoted, under one lexical rule for
  every plain scalar (`frontmatter.syntax`). `body.dynamic-injection` and
  `body.absolute-path` read the whole entry file, frontmatter included.
  The walk bounds (files, directories, depth, bytes, entries per
  directory) stop validation with one `file.size` finding; a file over
  1 MiB, a manifest over 64 KiB and an SPDX expression over 64 tokens or
  8 levels are each a finding of their own. The root entries `jobs/`,
  `libs/`, `variants/` and `llms.txt` are reserved and refused; a reserved
  entry is added by a capability token a set carrying it declares in
  `requires_capabilities`, so a reader without the token refuses the set
  whole. `format=2` is reserved for a change in what an existing entry
  means.
- Portable names (`file.name`): a file or directory name is of the POSIX
  portable filename character set, does not open with `-`, and is at most
  255 bytes, the bound `ai-tools-base` reads a name under. The finding
  proposes a portable name (NFKD, combining marks dropped, other runs as
  `_`); it does not rename.
- Relative links (`body.relative-link`): an inline link, image or reference
  definition in `SKILL.md` names a file of the same skill, and one in a
  subagent file names that file; a link inside a fenced block or a code
  span is not read.
- Safe reads and writes: a command opens a repository's files from a
  checked directory descriptor without following a symbolic link, refuses a
  file with a second hard link, and reads each file once; a linked `sets/`
  directory, set or manifest is refused before its target is read. A file
  `build-set` or `link-set` writes is replaced as a directory entry after
  the same link checks, never written into. `build-set` stages the bytes
  the validator
  read and replaces only a build its marker names as the same set;
  `link-set` validates the set before placing a skill and removes an entry
  only while it still matches its record.
- The commands: `validate`, `new-set`, `new-asset`, `sync-manifests`,
  `build-set`, `link-set`, `check-licenses`, `check-signoff` and
  `check-publisher`. Python 3.9, standard library only.
- The SPDX allowlist: every licence a set declares is on the list in force,
  a GPLv3-compatible permissive default that `publisher.conf` `licenses=`
  replaces; `check-licenses --allow` and `--exception` widen one run.
  `check-licenses` holds every file to it under REUSE 3.2 precedence and
  glob grammar, reading every header outside a `REUSE-IgnoreStart` block
  and a `.license` sidecar in a file's place, and reports a `REUSE.toml`
  outside the TOML subset it parses as one finding without judging any
  file on it.
- Dynamic injection: a skill or subagent may run a command when it loads
  once its `asset.conf` declares `skills.dynamic.v1` in
  `requires_capabilities` and `publisher.conf` sets
  `allow_dynamic_injection=yes`; every other substitution, and a
  declaration the publisher does not allow, fails `body.dynamic-injection`.
- `check-signoff`: the Developer Certificate of Origin check over a commit
  range, the trailer matching the author.
- Conformance fixtures under `fixtures/`: passing sets for the `acme` and
  `core` publisher shapes, and a failing or warning set for every rule a
  git tree holds, each naming its rule in `fixture.conf`; the tests build
  the remaining cases (a hard link, a special file, an over-size set) in a
  temporary directory.
- The reusable `validate` workflow: the validator, `sync-manifests --check`,
  `check-licenses`, `reuse lint`, `shellcheck` and `check-signoff`, run
  from the repository and commit that define the workflow file
  (`job.workflow_repository`, `job.workflow_sha`). Inputs:
  `license-exceptions`, `enforce-signoff` (the sign-off check is reported
  until a caller sets it) and `tools-repository` for a fork. GitHub
  Enterprise Server lacks those job fields, so a publisher there runs the
  commands from a local pin.
- This repository's release workflow for a `v<semver>` tag: it verifies
  the signed tag once, holds the captured tag object, confirms the remote
  still names it before publishing, and publishes the signed archive
  (`.sha256`, `.asc`) a consumer pins and the signed RPM
  `ai-tools-assets-tools`, which installs the tools, the format, the
  fixtures and the formatters with their licence texts under
  `/usr/share/ai-tools-assets-tools/` and asks `dag-node/rpm` to publish
  it. A prerelease tag creates a prerelease and does not dispatch. The `v1`
  tag is moved by hand.
- The reusable `set-release` workflow for a tag `<set>/v<semver>`: it
  verifies the signed tag, validates and builds the set, signs the zip and
  `SHA256SUMS` with the org package-signing key, ships the set as the
  signed RPM `ai-tools-assets-<set>`, attaches them to the GitHub release
  and, with `dispatch-rpm`, has `dag-node/rpm` publish the RPM. Every file
  in a set RPM and in the tools RPM carries the tag commit's timestamp, so
  two builds of one tag install the same payload. See `packaging/README.md`.
- The formatters from ai-tools-base under `formatters/`, `AGPL-3.0-only`;
  `format.sh` and `fill-markdown.py` take the checker as `--checker <path>`.

### Changed

- `check-publisher` reads the repository from `--root` (the current directory
  by default) and the reserved words beside itself, since it no longer lives
  in the repository it checks.
- `publisher.conf` carries `description=`, the marketplace's description,
  which `sync-manifests` renders.
