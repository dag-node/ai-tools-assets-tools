# Changelog

Changes to the tooling, released as tags `v<version>`; `v1` follows the latest
release of the major. The format follows [Keep
a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.5.1] - 2026-10-16

### Fixed

- **`new-set` names the set's dnf package and its plugin apart.**
  The `README.md` and `CHANGELOG.md` it writes name the dnf package
  `ai-tools-assets-sets-<set>` and the agent marketplace plugin
  `ai-tools-assets-<set>`, and the `README.md` holds the summary as given.
  Before, both names were the same, `ai-tools-assets-<set>`,
  and the `README.md` held the summary in the quotes `set.conf` needs. The dnf
  package name carries the `-sets-` infix, so it does not clash
  with the tooling package `ai-tools-assets-tools` and sorts beside the other
  `ai-tools` packages.

## [1.5.0] - 2026-10-15

### Changed

- **A set's RPM and zip are named `ai-tools-assets-sets-<set>`.** A set
  release attaches `ai-tools-assets-sets-<set>-<version>.zip` and
  `ai-tools-assets-sets-<set>-<version>-1.<dist>.noarch.rpm`, and `build-set`
  writes the zip under that name. The RPM provides `ai-tools-assets-<set>` at
  its own version and obsoletes every lower version of it, so `dnf upgrade`
  replaces an installed `ai-tools-assets-<set>` in one transaction and
  `dnf install ai-tools-assets-<set>` installs the new package. The plugin
  keeps the name `ai-tools-assets-<set>`. Before, the RPM and the zip were
  named `ai-tools-assets-<set>`, which for a set named `tools` is the name of
  this repository's RPM; the set `tools` does not carry the two relations.
- **The verify containers print each RPM's relations.** `sign-rpm` prints
  a `provides:` and an `obsoletes:` line for each relation of an RPM, as
  the rpm of the distribution its dist tag names reads it.
- **`sign-rpm` does not import the secret key on the runner.** It reads
  the public key its verify containers trust from the export the signing
  container writes, and keeps the pinned primary's key alone, so a release
  imports the secret key into a runner keyring in `preflight` and
  `sign-files` alone. Before, `sign-rpm` imported it a third time to export
  that public key.
- **The release log records the keyring removal.** At exit,
  `release-steps.sh` prints `removed the tmpfs scratch directory <name>` on
  stderr for each `/dev/shm` scratch directory it removed. Before, a removal
  printed nothing, and only a failed one printed a warning.
- **The verify containers print each mount's access.** `sign-rpm`'s verify
  containers print every mount with `ro` or `rw` and its type, as
  `job mount:` for `/`, `/in` and `/rpmdb` and `podman mount:` for the
  mounts podman adds. Before, they printed `tmpfs: <mountpoint>` for the tmpfs
  mounts alone.

## [1.4.0] - 2026-10-13

### Added

- **Suites tested for every caller.** The `validate` workflow's `tests` job
  runs a caller's `tests/` and each `sets/*/skills/*/tests` that holds a test
  module, at Python 3.9 and 3.12, and a caller gains the check runs
  `<job> / tests (python 3.9)` and `<job> / tests (python 3.12)`.
  Before, the job ran in this repository alone and a caller listed it
  as skipped.
- **The floor interpreter.** `release-steps.sh test-floor <directory>...` runs
  the suites inside the pinned EL9 image, on the `python3` EL9 ships,
  and refuses unless it is Python 3.9; the 3.9 leg of `validate`
  and the release run through it. Before, they ran the 3.9 build `setup-python`
  publishes.
- **A failed search for test directories stops the job.**
  `release-steps.sh find-tests` prints the directories the `tests` job runs,
  and refuses when a search fails.

### Changed

- **Fedora 44 image from Fedora's registry.** `FC44_IMAGE` names
  `registry.fedoraproject.org/fedora:44`, which keeps the digests it serves.
  Before, it named quay.io, which deletes a superseded digest within hours
  of the daily rebuild, and `check-images` failed every pull request here
  and in each caller from 2026-10-13.
- **Ubuntu 26.04.** Every job runs on `ubuntu-26.04`, and the release steps
  install `runc` where the runner image lacks it. Before, the jobs ran
  on `ubuntu-24.04`.
- **The validate and tests jobs are bounded.** A caller's `validate` job stops
  after 5 minutes, about twelve times its longest measured run. Each leg
  of the `tests` job stops after 5 minutes, about twice its slowest measured
  leg. Before, a hung `validate` step held the runner until GitHub's 6-hour job
  limit.
- **setup-python 7.** The `validate` job, the 3.12 leg of the `tests` job
  and the set release run `actions/setup-python` 7.0.0. Before, they ran 6.3.0.

### Security

- **Swap off for the signing key.** A release workflow runs
  `release-steps.sh swap-off` in a step that does not receive a secret,
  before the first step that does: it turns swap off and logs the memory
  and swap lines of `/proc/meminfo`. `preflight`, `sign-files`
  and `sign-rpm` each refuse to import the key while `/proc/swaps` lists
  a device. The runner process holds the job's secrets from before its first
  step, so a page of it written to swap before `swap-off` is outside the check.
- **Shared memory sized to its use.** The signing container's `/dev/shm` is 16
  MiB, a verify container has none, and its rpm database tmpfs is 32 MiB;
  `sign-rpms.sh` prints the size its scratch tree reached.
- **Verify containers without default writable mounts.** Each container that
  verifies an RPM's signature runs with `--read-only-tmpfs=false`, so podman
  does not add its writable tmpfs mounts to the read-only root, and the release
  log lists each tmpfs the container holds. Before, `/run`, `/tmp` and
  `/var/tmp` were writable in it.
- **The floor suite keeps its privilege drop.** `test-floor` runs the suites
  under `setpriv --no-new-privs` and refuses unless `/proc/self/status`
  shows `NoNewPrivs: 1`, so a set-id binary does not raise a suite's
  privileges inside the container.

## [1.3.1] - 2026-10-13

### Fixed

- **Signed re-pin commits.** `repin-tools` to a release signs its commit with
  the caller's own key where git signs commits, and gpg asks for the
  passphrase as for any signed commit. Before, gpg looked for that key in the
  keyring that verified the tag, and the commit failed with `No secret key`.
- **A failed command refuses.** `refresh-images` and `repin-tools` read the
  status of each git, `sha256sum` and `tail` call whose output decides a check,
  and a failure refuses with an `::error::` line naming what the run left: no
  commit, a local branch, or a pushed branch without its pull request. Before,
  a failed or cut-short answer could read as a clean tree, a commit that
  changes pins alone, or a replacement digest, and reach a pull request, and
  a warning git printed beside a successful `ls-remote` refused a re-pin.
- **Cleanup that fails.** The exit traps of `release-steps.sh` and
  `sign-rpms.sh` run every removal and print a warning naming one that fails.
  Before, a gpg-agent that did not stop, or a container that was not removed,
  ended the cleanup and left the scratch keyring under `/dev/shm`.

## [1.3.0] - 2026-10-13

### Added

- **Image check.** `release-steps.sh check-images` asks each registry for
  the manifest at every pinned image digest, and the `validate` workflow runs
  it on every pull request and push, here and in each caller. Before, an
  expired digest surfaced in the release run and cost the tag its version.
- **Caller pins agree.** In a caller, `validate` requires every `uses:` line
  naming these workflows to pin one ref, so the images it checks are the ones
  a release uses.
- **Digest refresh.** `release-steps.sh refresh-images` turns an expired
  digest into a pull request here, and `release-steps.sh repin-tools` re-pins
  each caller to the merged commit, or to a release, with a generated
  pull-request body. A refresh costs a pin in each caller, not a release. A
  caller pins a `develop` commit while the tools changed in pin lines and
  `CHANGELOG.md` alone since the caller's current pin; any other change
  reaches it through a release, and `repin-tools` says so. `--help` prints
  their usage.

### Changed

- **Images pulled before signing.** The preflight step pulls every image, and
  the containers run those copies alone, so an image retired after the check
  stops a release before it signs anything.
- **No bytecode cache in a run.** The workflows run Python with
  `PYTHONDONTWRITEBYTECODE=1`, so a run leaves the caller's checkout and the
  tools checkout holding their committed files alone.

### Security

- **Verify containers without capabilities.** The containers that verify each
  RPM's signature run with no capabilities, no new privileges, a read-only
  root and a bound on their processes.

## [1.2.0] - 2026-10-12

### Changed

- **Fixtures out of the RPM.** The RPM `ai-tools-assets-tools` does not install
  the conformance fixtures, and an upgrade removes
  `/usr/share/ai-tools-assets-tools/fixtures`. The release archive keeps them
  for a validator held to them. An installed tool does not read them.

### Fixed

- **Fedora 44 image.** A release verifies its Fedora 44 RPM in the image
  quay.io serves for `fedora:44` on 2026-10-12. quay.io expired the digest
  1.1.0 pinned, so a release run from 1.1.0 stops at that check
  with `manifest unknown`.
- **Hanging paragraphs.** `formatters/fill-comments.sh` refills a contract
  head such as `name <args> : text` and the lines indented under it as one
  paragraph, its first line keeping the head's prefix. Such a paragraph was
  left over the column.
- **Two-column blocks.** The comment filler leaves a block as written whose
  rows a gap of two spaces splits, or a ` -- ` at one column from a line that
  opens a sentence. It joined the block's first row to the paragraph before it.
- **Column tables.** The comment filler wraps a cell that runs past the column
  at the cell's own indent. It re-pads a two-column table whose first column
  a rename widened or narrowed, and leaves a table of three columns as written.
- **Decorated headers.** The comment filler shortens the trailing rule of
  a header such as `# ── title ───` to bring it inside the column, and does
  not wrap it.
- **Rule characters in a code span.** The comment filler fills a paragraph
  holding a span such as `` `*** ... File:` ``. It read the line as a banner
  and left the paragraph over the column.
- **Every reflow difference.** `formatters/verify-reflow.py` reports each
  difference in a file with its line in the base and in the tree. It reported
  the first alone.
- **Respaced lines.** The reflow gate reports a protected line whose spacing
  alone changed as `respaced line`, apart from a damaged one, and fails like
  it.
- **Column alignment.** The reflow gate holds a comment's column table to its
  first column and its alignment. A column a fill broke fails, and a table
  re-padded at another column passes.

## [1.1.0] - 2026-10-09

### Changed

- `dnf install ai-tools-assets-tools` installs the tooling from rpm.dagnode.com
  on EL 9, EL 10 and Fedora 44. Each release builds one RPM per distribution,
  `<name>-<version>-1.<dist>.noarch.rpm` with `<dist>` one of `el9`, `el10`
  and `fc44`, and the repository serves each from the tree of its distribution.
  A set released through `set-release.yml` is built and served the same way,
  so its package installs with `dnf` too. The 1.0.0 RPM carries the Release `1`
  without a distribution, so the repository does not serve it; it stays
  attached to the 1.0.0 GitHub release.

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
