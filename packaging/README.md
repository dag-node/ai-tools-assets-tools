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

The job reads the set and the version from the tag, verifies the tag against
the pinned maintainer keys, runs `tools/validate`, `tools/check-publisher
--repository` and `tools/build-set`, signs `SHA256SUMS` and the zip with the
org package-signing key, builds the RPM `ai-tools-assets-<set>` with nFPM
once for each distribution `rpm.dagnode.com` serves (EL9, EL10, Fedora 44),
signs the RPMs in an EL9 container and verifies each in its own distribution,
and creates the GitHub release. With `dispatch-rpm: true` it then asks
`dag-node/rpm` to publish the RPMs at `rpm.dagnode.com`; a prerelease tag (`<set>/v1.2.0-rc.1`)
creates a prerelease and does not dispatch. The set's `version` in `set.conf`
must equal the tag's.

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

## The release environment

The called job runs under the calling repository's `release` environment.
Create it before the first tag is pushed, with a deployment policy that admits
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
| `ai-tools-assets-<set>-<version>.zip` | the staged set, plugin root at the top, as `tools/build-set` wrote it |
| `….zip.sha256`, `….zip.asc` | its SHA-256 and its detached signature |
| `SHA256SUMS`, `SHA256SUMS.asc` | the staged set's inventory and its detached signature |
| `ai-tools-assets-<set>-<version>-1.<dist>.noarch.rpm` | the staged set under `/usr/share/ai-tools-assets/<set>/`, `SHA256SUMS.asc` included, with an RPM header signature; one per `<dist>` of `el9`, `el10` and `fc44`, which `dag-node/rpm` places in the tree of that distribution |

The zip does not carry `SHA256SUMS.asc`, since `build-set` writes the zip
before the release signs `SHA256SUMS`; the zip's own `.asc` covers it whole.

## Files

| File | What it does |
|---|---|
| `release-steps.sh` | one subcommand per release step, shared by `set-release.yml` and this repository's `release.yml`; lists the distributions an RPM is built for |
| `render-nfpm.py` | writes the nFPM configuration of a set's RPM for one distribution from `nfpm-set.yaml.in`, the staged `set.conf` and `publisher.conf`, or of the tools RPM from `nfpm-tools.yaml.in` |
| `nfpm-set.yaml.in` | the package a set ships as |
| `nfpm-tools.yaml.in` | the package this repository's tools ship as, built by `release.yml` from the release archive |
| `sign-rpms.sh` | signs and verifies an RPM inside the EL container; a copy from ai-tools-base, `AGPL-3.0-only` |
