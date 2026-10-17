# Packaging

A publisher releases a set by pushing a signed tag `<set>/v<semver>`. Its
`.github/workflows/release.yml` calls the reusable set release:

```yaml
on:
  push:
    tags: ['*/v*']

permissions:
  contents: read

jobs:
  ai-tools-assets:
    uses: dag-node/ai-tools-assets-tools/.github/workflows/set-release.yml@v1
    permissions:
      contents: write
    secrets: inherit
    with:
      dispatch-rpm: true
```

The job reads the set and the version from the tag, verifies the tag
against the pinned maintainer keys, runs `tools/validate`,
`tools/check-publisher --repository` and `tools/build-set`, signs `SHA256SUMS`
and the zip with the org package-signing key, builds the RPM
`ai-tools-assets-sets-<set>` with nFPM once for each distribution
`rpm.dagnode.com` serves (EL9, EL10, Fedora 44), signs the RPMs in an EL9
container and verifies each in its own distribution, and creates the GitHub
release. With `dispatch-rpm: true` it then asks `dag-node/rpm` to publish
the RPMs at `rpm.dagnode.com`; a prerelease tag (`<set>/v1.2.0-rc.1`) creates
a prerelease and does not dispatch. The set's `version` in `set.conf` must
equal the tag's.

## Inputs

| Input | Default | What it names |
|---|---|---|
| `dispatch-rpm` | `false` | send `publish-rpm` to `dag-node/rpm` once the release exists |
| `tag-signer-fingerprints` | p4nda's primary | the primaries a release tag may be signed by, space-separated |
| `tag-signer-key-url` | `https://github.com/p4nda.gpg` | where their public keys are fetched for the tag check |
| `artifact-signer-fingerprint` | the dag-node package-signing primary | the primary of the key in `GPG_SIGNING_KEY` |
| `tools-repository` | `dag-node/ai-tools-assets-tools` | a fork the tools are checked out from, as `<owner>/<repo>` |

`@v1` follows the latest release of the tools; a full commit SHA in its place
holds the caller to one reviewed version until the caller changes it.

## Before a release tag

Run the image check immediately before `git tag -s`. In this repository, at
the commit the tag will name:

```bash
bash packaging/release-steps.sh check-images
```

In a caller, from the caller's checkout, with the tools checked out beside it
at the commit its `uses:` lines pin:

```bash
tools=../ai-tools-assets-tools
bash "$tools/packaging/release-steps.sh" check-images
```

It asks each image's registry for the manifest at the pinned digest, prints
one line per image, and exits non-zero when a digest no longer resolves,
naming the digest the tag resolves to now. In a caller it also requires every
`uses:` line naming these workflows to carry one ref. The `validate` workflow
runs the same check on every pull request and push. A release run pulls every
image in its preflight step, before the first signature, so an image retired
after the check stops the run before it signs anything.

## Tests at the Python floor

The `validate` workflow's `tests` job runs a caller's Python suites, `tests/`
at the root and each `sets/*/skills/*/tests` that holds a `test*.py`, which
`release-steps.sh find-tests` prints one per line; a search that fails stops
the job. Its `tests (python 3.9)` leg runs them on the runner with:

```bash
bash "$TOOLS/packaging/release-steps.sh" test-floor sets/core/skills/ai-tools-reftags/tests
```

`test-floor` runs the suites in one container of the EL9 image the release
steps pin, with the checkout mounted read-only. It installs `git`, refuses
unless the image's `python3 --version` is Python 3.9, and runs
`python3 -m unittest discover -s <directory> -v` for each directory
as the runner's own user, with `no_new_privs` set, so a set-id binary does
not raise a suite's privileges. This repository's release runs its suite
the same way, before the first step that receives a secret. The step sets
runc as podman's runtime under `/etc/containers/containers.conf.d/`, so it is
a runner step and not a command for a workstation.

## When an image digest expires

A registry can retire an image's digest once its tag moves on, and from then
on `check-images` fails every pull request in this repository and in each
caller. Fedora's quay.io organization deletes a superseded digest within hours
of its daily rebuild, so the Fedora 44 pin names
`registry.fedoraproject.org/fedora:44`, which serves the same digests and keeps
them. A refresh is a commit on this repository's `develop`, not a release,
carried by `release-steps.sh refresh-images` here and `repin-tools` in each
caller; [docs/development/release.md](../docs/development/release.md) states
the procedure.

## The release environment

The called job runs under the calling repository's `release` environment.
Create it before the first tag is pushed, with a deployment policy that allows
tags `*/v*` and no branch: GitHub creates an environment a workflow names and
the repository lacks, with no protection rule, and `secrets: inherit` hands the
job every secret the repository holds. The environment holds three secrets:

| Secret | Value |
|---|---|
| `GPG_SIGNING_KEY` | the armored export of the package-signing subkey |
| `GPG_SIGNING_PASSPHRASE` | its passphrase, on one line |
| `RPM_REPO_DISPATCH_TOKEN` | a fine-grained token with `contents: write` on `dag-node/rpm`, read only with `dispatch-rpm: true` |

`secrets: inherit` is how they reach the job: a job that calls a workflow
cannot declare an environment, so it cannot pass an environment secret by name.
The status check is `ai-tools-assets / release`, from the calling job's name.

## What a release attaches

| Asset | What it is |
|---|---|
| `ai-tools-assets-sets-<set>-<version>.zip` | the staged set, plugin root at the top, as `tools/build-set` wrote it |
| `….zip.sha256`, `….zip.asc` | its SHA-256 and its detached signature |
| `SHA256SUMS`, `SHA256SUMS.asc` | the staged set's inventory and its detached signature |
| `ai-tools-assets-sets-<set>-<version>-1.<dist>.noarch.rpm` | the staged set under `/usr/share/ai-tools-assets/<set>/`, `SHA256SUMS.asc` included, with an RPM header signature; one per `<dist>` of `el9`, `el10` and `fc44`, which `dag-node/rpm` places in the tree of that distribution |

The zip does not carry `SHA256SUMS.asc`, since `build-set` writes the zip
before the release signs `SHA256SUMS`; the zip's own `.asc` covers it whole.

A set's RPM was named `ai-tools-assets-<set>` up to tools 1.4.0. The RPM
provides that name at its own version and obsoletes every lower version of it,
so `dnf upgrade` replaces an installed package of the old name in one
transaction and `dnf install ai-tools-assets-<set>` installs the new one.
The set `tools` carries neither relation, since `ai-tools-assets-tools` is
the tools RPM.

## Files

| File | What it does |
|---|---|
| `release-steps.sh` | one subcommand per release step, shared by `set-release.yml` and this repository's `release.yml`, and `find-tests` and `test-floor` for `validate.yml`'s `tests` job; lists the distributions an RPM is built for |
| `render-nfpm.py` | writes the nFPM configuration of a set's RPM for one distribution from `nfpm-set.yaml.in`, the staged `set.conf` and `publisher.conf`, or of the tools RPM from `nfpm-tools.yaml.in` |
| `nfpm-set.yaml.in` | the package a set ships as |
| `nfpm-tools.yaml.in` | the package this repository's tools ship as, built by `release.yml` from the release archive |
| `sign-rpms.sh` | signs and verifies an RPM inside the EL container; a copy from ai-tools-base |
| `license-text-digests.py` | prints the lines of the licence table in `tools/lib/license_texts.py` from spdx/license-list-data: each licence's class as the table holds it, its OSI and FSF flags, and the digest of every published revision of its text since `--floor`; a maintainer runs it when a licence joins the table or SPDX publishes a list |
