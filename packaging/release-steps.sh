#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# The steps of a signed release, one subcommand per workflow step, shared by this repository's release.yml and the
# reusable set-release.yml a publisher calls, so the tag check, the key handling, the RPM signing and the dispatch exist
# in one copy. Each subcommand exits non-zero on the first failed check, with a message carrying GitHub's `::error::`
# prefix, and a release step after it does not run.
#
# ```bash
# bash packaging/release-steps.sh check-images
# bash <tools>/packaging/release-steps.sh find-tests
# bash <tools>/packaging/release-steps.sh test-floor <directory>...
# bash packaging/release-steps.sh refresh-images --id <ID> [--caller <path>]... [--dry-run]
# bash <tools>/packaging/release-steps.sh repin-tools --id <ID> <sha>|<vX.Y.Z> [--pr <number>] [--dry-run]
# bash packaging/release-steps.sh swap-off
# bash packaging/release-steps.sh verify-tag <tag> <event-commit>
# bash packaging/release-steps.sh preflight
# bash packaging/release-steps.sh sign-files <file>...
# bash packaging/release-steps.sh build-rpm <output-directory> <render-nfpm.py arguments>...
# bash packaging/release-steps.sh sign-rpm <rpm>...
# bash packaging/release-steps.sh create-release <tag> <notes> <file>...
# bash packaging/release-steps.sh dispatch <tag>
# ```
#
# Environment, by subcommand (check-images, find-tests, test-floor, refresh-images, repin-tools and swap-off read
# none; refresh-images and repin-tools use the operator's own git and gh credentials, and `--help` prints their usage):
#   verify-tag      TAG_SIGNER_PRIMARY_FINGERPRINTS (space-separated), TAG_SIGNER_KEY_URL
#   preflight       GPG_SIGNING_KEY, GPG_SIGNING_PASSPHRASE, ARTIFACT_SIGNER_PRIMARY_FINGERPRINT; DISPATCH_RPM=true adds
#                   RPM_REPO_DISPATCH_TOKEN
#   sign-files      GPG_SIGNING_KEY, GPG_SIGNING_PASSPHRASE, ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
#   build-rpm       SOURCE_DATE_EPOCH
#   sign-rpm        GPG_SIGNING_KEY, GPG_SIGNING_PASSPHRASE, ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
#   create-release  GH_TOKEN, RELEASE_TAG_OBJECT (written by verify-tag)
#   dispatch        RPM_REPO_DISPATCH_TOKEN
#
# check-images asks each image's registry for the manifest at its pinned digest with `skopeo inspect --raw`, which does
# not pull a layer, and prints one line per image. A digest the registry reports unknown fails with the digest the tag
# names now, and a registry it could not read fails without naming one; a reference without a digest is refused. Run in
# a caller's checkout, it first requires every `uses:` line naming a workflow of dag-node/ai-tools-assets-tools to carry
# one `@<ref>`, a textual check, and a failed image adds that the tools' develop may already carry the refresh. It does
# not read a secret, so it runs on a pull request from a fork.
#
# test-floor runs the Python suites of the current checkout at the floor the tools and the skills are written
# for: in one container of EL9_IMAGE, with the checkout mounted read-only, it installs git, prints `python3 --version`
# and refuses unless the image's interpreter is Python 3.9, then runs `python3 -m unittest discover -s <directory> -v`
# for each directory as the calling user's uid and gid (setpriv), so a test that expects a permission refusal or a git
# check of the checkout's owner sees the runner's user and not root. setpriv sets no_new_privs, so a set-id bit or
# a file capability on a binary a suite runs does not raise its privileges; a control run through the same setpriv
# prints the NoNewPrivs line of /proc/self/status and refuses unless it is 1. It exits non-zero when any directory's
# suite fails. The container keeps the network for the install and does not receive a secret, and the release runs it
# before the first step that does. find-tests prints the directories validate.yml passes to it, one per line: tests
# and each sets/*/skills/*/tests that holds a file test*.py, since unittest discovery exits non-zero over a directory
# without one from Python 3.12; a `find` that fails refuses, so a failed search does not read as none found.
#
# refresh-images and repin-tools carry a digest refresh, one half each: refresh-images rewrites the expired pins here
# and opens their pull request, and repin-tools rewrites a caller's `uses:` lines to the merged commit, or to a release
# of the tools whose tag verify-tag would accept and whose release run passed, and opens the caller's. Both run under
# the operator's own git and gh credentials, so push rights are the permission and each merge stays the operator's.
# A develop commit is pinned only when it changes image pin lines and CHANGELOG.md alone; any other change reaches
# a caller through a release. A command whose output decides a check is read with its status first, through
# _release_steps__read_output or an assignment followed by a refusal, so a failed one refuses instead of reading
# as an empty or partial answer. A refusal names what the run left: no commit, a local branch that is not pushed,
# or a pushed branch whose pull request is not open; the same command run again continues from it.
#
# verify-tag fetches the tag from origin and holds it to an annotated tag object that names itself, peels to the event's
# commit and the checkout's HEAD, and carries a signature whose VALIDSIG primary is on the pinned list; the key is
# fetched from TAG_SIGNER_KEY_URL for that one check, so a leaked CI secret cannot mint a release by itself. It appends
# RELEASE_COMMIT and RELEASE_TAG_OBJECT to $GITHUB_ENV, and every later step names that object or that commit.
# create-release compares the remote's refs/tags/<tag> with RELEASE_TAG_OBJECT immediately before `gh release create
# --verify-tag`, which checks that the name exists, not which object it names.
#
# The signing key is imported into a keyring under /dev/shm, which must be tmpfs; a /dev/shm that is absent or not tmpfs
# refuses before a directory is created, and the EXIT trap stops the keyring's gpg-agent and removes it. swap-off runs
# `sudo swapoff -a`, refuses while /proc/swaps then lists a device, and logs the memory and swap lines of /proc/meminfo;
# a release workflow runs it in a step that does not receive a secret, before the first step that does.
# preflight, sign-files and sign-rpm each refuse before the key enters a process while /proc/swaps lists a device, so
# a page holding the key or the passphrase is not written to disk by this job's steps. The runner process holds
# the job's secrets from before its first step, so a page of it written to swap before swap-off is outside this check.
# preflight refuses an imported key whose primary is not ARTIFACT_SIGNER_PRIMARY_FINGERPRINT, and sign-files and
# sign-rpm verify against that primary's public key alone, so a signature by another key fails the step that made it.
# Every download is HTTPS alone and bounded in time, so a redirect to another scheme or a stalled server fails the step.
#
# build-rpm runs nFPM at NFPM_VERSION and NFPM_TARBALL_SHA256, downloaded and checked before it runs, and builds one
# RPM without a signature per distribution DIST_IMAGES lists, each from the configuration render-nfpm.py writes for
# that dist tag; dag-node/rpm places a package by the dist tag in its file name, so a distribution rpm.dagnode.com
# serves is added to DIST_IMAGES and to dag-node/rpm's served trees together. SOURCE_DATE_EPOCH fixes the header's
# timestamps and those of a `dir` or `file` entry, while a file under a `tree` entry keeps its staged mtime, which
# the calling workflow sets to SOURCE_DATE_EPOCH. sign-rpm installs rpm-sign and gnupg2 in an EL9 container (the oldest
# rpm the RPMs install with) that does not receive the secrets, so no package scriptlet runs while the key is present,
# signs every RPM with sign-rpms.sh beside this file in one container of that image, copies the signed files out with
# `podman cp`, and requires `rpmkeys -Kv` to print a signature line ending in OK for each RPM inside the image of the
# distribution its dist tag names; the exit status alone passes an unsigned package. The secrets reach a container
# on stdin, since podman records an `-e` value in the container's configuration on disk. The container that holds
# the key and those that verify run with no network, so the key does not leave them over a network; the signed RPMs
# do leave the signing container and are published, so the digest pins are what fix the code that runs with the key.
# check-images names the digest to take when the registry retires a pin. preflight pulls every image after its key and
# token checks, ahead of the first signature, and each container runs with `--pull=never`, so an image the registry
# retires between check-images and the run stops the run before a signature or a version is spent. Each verify
# container runs as root with `--cap-drop=all`, no new privileges, a read-only root without the writable tmpfs mounts
# podman adds to one by default (`--read-only-tmpfs=false`), a bound on its processes and the rpm database on a tmpfs,
# since the step's environment on the runner holds the secrets, and prints each tmpfs it holds; it mounts a directory
# holding the public key and the signed RPMs alone, which root reads without the capability that bypasses a directory's
# mode. The containers run through `sudo podman` with runc as the OCI runtime, which the GitHub runner needs: its
# default crun rejects the generated OCI spec. ensure_podman installs runc where the runner image lacks it.
# el-repos.sh points the EL9 container's repos at one ordered host list before its `dnf install`.
#
# Each container's shared memory is sized to its use. The signing container's /dev/shm, which holds the keyring,
# the passphrase file and the rpm macros sign-rpms.sh writes, is 16 MiB, and sign-rpms.sh prints the size its scratch
# tree reached. A verify container has no /dev/shm (`--ipc=none`), and its rpm database is a 32 MiB tmpfs.
set -euo pipefail

NFPM_VERSION=2.47.0
NFPM_TARBALL_SHA256=0660ca602b2d2d2ae4781a06c692b3eeb9d437ffea05b831d76e41f4a3188783
# The multi-arch index digest of each image, each line ending in the date it was resolved. Every image is pinned
# by digest: EL9_IMAGE runs with the signing key and its output is published, and each verify image runs as root
# on the runner that holds the secrets; check-images refuses an entry without a digest. Fedora's quay.io organization
# deletes a superseded digest within hours of its daily rebuild, so FC44_IMAGE names Fedora's own registry, which serves
# the same digests and keeps them. Where a registry retires a pinned digest, refresh-images rewrites the line
# to the digest the tag resolves to, as a commit on develop that each caller pins with repin-tools, and the next release
# carries it.
EL9_IMAGE=quay.io/rockylinux/rockylinux:9@sha256:8101994123cf3d0a8fee517bee7f39e555c7d92bd2d9eb3303cc988a0eeed00f # resolved 2026-10-08
EL10_IMAGE=quay.io/rockylinux/rockylinux:10@sha256:827d37bc128288ccf160ee318bb3cb92d591164cb217e92f8bc61e3982ae1834 # resolved 2026-10-08
FC44_IMAGE=registry.fedoraproject.org/fedora:44@sha256:959833699a2fb5da1bc70b4db659ab229252c2ba1ed8e2fa29d4b30a704c0f63 # resolved 2026-10-13
# The distributions an RPM is built for, by dist tag, each with the image its signature is verified in.
declare -A DIST_IMAGES=([el9]="$EL9_IMAGE" [el10]="$EL10_IMAGE" [fc44]="$FC44_IMAGE")
# The name each distribution takes in the commit, the pull request and the changelog entry of a digest refresh.
declare -A _RELEASE_STEPS__DIST_NAMES=([el9]="Rocky 9" [el10]="Rocky 10" [fc44]="Fedora 44")
# Every curl: HTTPS alone, no scheme change on a redirect, and a bound on the whole transfer.
CURL=(curl -sSf --proto '=https' --proto-redir '=https' --max-time 120)
SIGN_IMAGE=localhost/ai-tools-assets-sign:el9
PACKAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# One pattern for the line rpm 4 and rpm 6 print for a valid signature; a digest-only line does not match.
SIGNATURE_OK_PATTERN='signature.*:[[:space:]]*OK[[:space:]]*$'
# An image reference check-images accepts: <registry>/<path>:<tag>@sha256:<digest>, the tag kept for the refresh.
_RELEASE_STEPS__IMAGE_REFERENCE_PATTERN='^([a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+):([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})@(sha256:[0-9a-f]{64})$'
# The registry's answer for a digest it no longer serves; any other failure is a registry check-images could not read.
_RELEASE_STEPS__MANIFEST_UNKNOWN_PATTERN='manifest unknown|MANIFEST_UNKNOWN'
# The image pins check-images reads and what it found, set by the functions that name them.
_RELEASE_STEPS__PIN_DISTS=()
declare -A _RELEASE_STEPS__PIN_REFERENCES=()
_RELEASE_STEPS__EXPIRED_DISTS=()
declare -A _RELEASE_STEPS__CURRENT_DIGESTS=()
_RELEASE_STEPS__FAILED_COUNT=0
_RELEASE_STEPS__SERVED_DIGEST=""
_RELEASE_STEPS__BRANCH_DATE=""
_RELEASE_STEPS__ANSWER=""
# The repository whose workflows a caller pins, and each `uses:` line of a caller that names one, by position.
_RELEASE_STEPS__TOOLS_REPOSITORY=dag-node/ai-tools-assets-tools
_RELEASE_STEPS__CALLER_FILES=()
_RELEASE_STEPS__CALLER_LINE_NUMBERS=()
_RELEASE_STEPS__CALLER_PREFIXES=()
_RELEASE_STEPS__CALLER_WORKFLOWS=()
_RELEASE_STEPS__CALLER_REFS=()
_RELEASE_STEPS__CALLER_COMMENTS=()
# The tag object and its signer that verify-tag and repin-tools accept.
_RELEASE_STEPS__TAG_OBJECT=""
_RELEASE_STEPS__TAG_PRIMARY=""

# A refusal; while refresh-images checks a caller, the framing names the caller and that the tools are unchanged.
_RELEASE_STEPS__DIE_PREFIX=""
_RELEASE_STEPS__DIE_SUFFIX=""
_release_steps__die() {
    echo "::error::release-steps: $_RELEASE_STEPS__DIE_PREFIX$*$_RELEASE_STEPS__DIE_SUFFIX" >&2
    exit 1
}

# Every scratch directory and the signing container are removed on exit, a gpg-agent started in a scratch directory is
# stopped first, and INT and TERM exit through the same trap. Each removal runs whatever the one before it answered,
# and one that fails prints a warning naming what it left, so a container that is not removed does not keep a keyring
# directory under /dev/shm from removal. Each tmpfs scratch directory removed is named by its basename, so the log
# records a keyring's removal; the temporary directory, which holds public data, is removed without a line. The line
# goes to stderr, since the trap prints after the subcommand and a subcommand's stdout can be its caller's data.
# The helpers set globals rather than print, since state set inside a command substitution's subshell does not reach
# this shell's trap.
scratch_directories=()
container=""
_RELEASE_STEPS__TEMP_DIR=""
_RELEASE_STEPS__WORKTREE=""
_RELEASE_STEPS__WORKTREE_REPOSITORY=""
_release_steps__remove_scratch_and_container() {
    local directory
    if [[ -n "$container" ]] && ! sudo podman rm -f "$container" >/dev/null 2>&1; then
        _release_steps__warn "the container $container was not removed; remove it with \`sudo podman rm -f $container\`"
    fi
    if [[ -n "$_RELEASE_STEPS__WORKTREE" ]]; then
        git -C "$_RELEASE_STEPS__WORKTREE_REPOSITORY" worktree remove --force \
            "$_RELEASE_STEPS__WORKTREE" >/dev/null 2>&1 || true
    fi
    for directory in "${scratch_directories[@]}"; do
        # Only the agent of a directory holding a secret keyring can hold a key, so a failed stop is warned of there.
        if ! GNUPGHOME="$directory" gpgconf --kill gpg-agent >/dev/null 2>&1 && [[ -d "$directory/private-keys-v1.d" ]]; then
            _release_steps__warn "the gpg-agent of $directory did not stop; stop it with \`GNUPGHOME=$directory gpgconf --kill gpg-agent\`"
        fi
        if rm -rf "$directory"; then
            # An unbound read under `set -u` would end the trap before the removals after it.
            [[ "$directory" == "${_RELEASE_STEPS__TEMP_DIR:-}" ]] \
                || echo "release-steps: removed the tmpfs scratch directory ${directory##*/}" >&2
        else
            _release_steps__warn "$directory was not removed; remove it with \`rm -rf $directory\`"
        fi
    done
}
trap _release_steps__remove_scratch_and_container EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# _release_steps__create_scratch: set `scratch` to a fresh directory under /dev/shm. A /dev/shm that is absent
# or not tmpfs refuses before anything is created there, and the directory itself is checked after, so a key, a
# passphrase or a token does not reach a disk-backed directory.
_release_steps__create_scratch() {
    [[ -d /dev/shm && "$(stat -f -c %T /dev/shm 2>/dev/null)" == tmpfs ]] \
        || _release_steps__die "/dev/shm is absent or not tmpfs"
    scratch="$(mktemp -d -p /dev/shm)" || _release_steps__die "no directory could be created under /dev/shm"
    scratch_directories+=("$scratch")
    [[ "$(stat -f -c %T "$scratch")" == tmpfs ]] || _release_steps__die "$scratch is not on tmpfs"
}

# _release_steps__create_temp_dir: set _RELEASE_STEPS__TEMP_DIR to one ordinary temporary directory per run, for
# data that is public (a manifest, a rewritten file, a command's diagnostics); a secret takes the tmpfs scratch.
_release_steps__create_temp_dir() {
    [[ -z "$_RELEASE_STEPS__TEMP_DIR" ]] || return 0
    _RELEASE_STEPS__TEMP_DIR="$(mktemp -d)" || _release_steps__die "no temporary directory could be created"
    scratch_directories+=("$_RELEASE_STEPS__TEMP_DIR")
}

# Refuse unless /proc/swaps holds its header line alone; a file that cannot be read refuses too.
_RELEASE_STEPS__PROC_SWAPS=/proc/swaps
_release_steps__require_swap_off() {
    local swaps
    swaps="$(cat -- "$_RELEASE_STEPS__PROC_SWAPS" 2>&1)" \
        || _release_steps__die "$_RELEASE_STEPS__PROC_SWAPS could not be read ($(_release_steps__format_one_line "$swaps")), so swap is not shown to be off; the signing key is not imported"
    [[ "$swaps" =~ ^Filename[[:space:]] && "$swaps" != *$'\n'* ]] \
        || _release_steps__die "$_RELEASE_STEPS__PROC_SWAPS does not hold its header line alone ($(_release_steps__format_one_line "$swaps")), so swap is not shown to be off; the signing key is imported only while it lists no device, which \`release-steps.sh swap-off\` sets with \`sudo swapoff -a\`"
}

_release_steps__swap_off() {
    [[ "$#" -eq 0 ]] || _release_steps__die "usage: swap-off"
    sudo swapoff -a || _release_steps__die "\`sudo swapoff -a\` failed; the signing steps refuse to import the key while swap may be on"
    _release_steps__require_swap_off
    echo "swap is off: $_RELEASE_STEPS__PROC_SWAPS lists no device"
    awk '/^(MemTotal|MemAvailable|Shmem|SwapTotal|SwapFree):/ { print "memory: " $1 " " $2 " " $3 }' /proc/meminfo \
        || _release_steps__warn "/proc/meminfo could not be read; the memory lines are not logged"
}

_release_steps__require_env() {
    local name
    for name in "$@"; do
        [[ -n "${!name:-}" ]] || _release_steps__die "$name is empty; in CI it is $(_release_steps__format_env_hint "$name")"
    done
}

_release_steps__format_env_hint() {
    case "$1" in
        GPG_SIGNING_KEY) echo "the signing-subkey export, a secret of the release environment: gh secret set GPG_SIGNING_KEY --env release < signing-subkey.asc" ;;
        GPG_SIGNING_PASSPHRASE) echo "the signing subkey's passphrase, a secret of the release environment: gh secret set GPG_SIGNING_PASSPHRASE --env release" ;;
        RPM_REPO_DISPATCH_TOKEN) echo "a fine-grained token with contents: write on dag-node/rpm, a secret of the release environment: gh secret set RPM_REPO_DISPATCH_TOKEN --env release" ;;
        *) echo "set by the workflow" ;;
    esac
}

# _release_steps__import_signing_key: import GPG_SIGNING_KEY into a fresh scratch keyring, export GNUPGHOME,
# set `signing_primary`. GNUPGHOME names that keyring for the rest of the process, so a subcommand that calls this does
# not make a signed git commit or tag after it, which would look for its key there. gpg's import diagnostics name key
# IDs and user IDs, which are public, and stay on stderr for a key that does not import.
_release_steps__import_signing_key() {
    _release_steps__create_scratch
    export GNUPGHOME="$scratch"
    printf '%s\n' "$GPG_SIGNING_KEY" | gpg --batch --quiet --import \
        || _release_steps__die "GPG_SIGNING_KEY did not import: it is not an ASCII-armored key block. Export the signing subkey with gpg --armor --export-secret-subkeys '<subkey-fpr>!'"
    gpg --batch --with-colons --list-secret-keys | grep -q '^sec:' \
        || _release_steps__die "GPG_SIGNING_KEY carries no secret key: it is a public key, or an export without the secret part"
    signing_primary="$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')"
}

# _release_steps__resolve_tag_object <repository-dir> <tag> [<revision>]: set _RELEASE_STEPS__TAG_OBJECT to
# the annotated tag object <revision> (refs/tags/<tag> where not given) names, once that object names itself <tag>.
_release_steps__resolve_tag_object() {
    local directory="$1" tag="$2" revision="${3:-refs/tags/$2}" tag_object embedded
    tag_object="$(git -C "$directory" rev-parse --verify --quiet "$revision^{tag}")" \
        || _release_steps__die "$tag is not an annotated tag; a release tag is created with git tag -s"
    embedded="$(git -C "$directory" cat-file -p "$tag_object" | awk '/^tag / { print $2; exit }')"
    [[ "$embedded" == "$tag" ]] \
        || _release_steps__die "the tag object names itself $embedded, not $tag; a tag object is published under its own name"
    _RELEASE_STEPS__TAG_OBJECT="$tag_object"
}

# _release_steps__verify_tag_signature <repository-dir> <tag> <tag-object> <fingerprints> <key-url>: refuse
# a tag object without a PGP signature, then import the signers' keys from <key-url> into a scratch keyring and require
# `git verify-tag` to report a VALIDSIG primary on the space-separated <fingerprints>. Sets
# _RELEASE_STEPS__TAG_PRIMARY. GNUPGHOME names the scratch keyring for the import and the check alone, so a signed
# `git commit` later in the same run reaches the caller's own keyring and agent.
_release_steps__verify_tag_signature() {
    local directory="$1" tag="$2" tag_object="$3" fingerprints="$4" key_url="$5" status primary
    git -C "$directory" cat-file -p "$tag_object" | grep -qx -- '-----BEGIN PGP SIGNATURE-----' \
        || _release_steps__die "$tag carries no PGP signature; a release tag is created with git tag -s"
    _release_steps__create_scratch
    "${CURL[@]}" "$key_url" | GNUPGHOME="$scratch" gpg --batch --quiet --import \
        || _release_steps__die "the tag signers' key did not import from $key_url"
    status="$(GNUPGHOME="$scratch" git -C "$directory" verify-tag --raw "$tag_object" 2>&1)" \
        || _release_steps__die "$tag does not verify: $status"
    primary="$(printf '%s\n' "$status" | awk '/^\[GNUPG:\] VALIDSIG/ { print $NF; exit }')"
    [[ -n "$primary" && " $fingerprints " == *" $primary "* ]] \
        || _release_steps__die "$tag is signed by primary ${primary:-none}, which is not on the pinned list: $fingerprints"
    _RELEASE_STEPS__TAG_PRIMARY="$primary"
}

_release_steps__verify_tag() {
    [[ "$#" -eq 2 ]] || _release_steps__die "usage: verify-tag <tag> <event-commit>"
    local tag="$1" event_commit="$2" tag_object commit
    _release_steps__require_env TAG_SIGNER_PRIMARY_FINGERPRINTS TAG_SIGNER_KEY_URL
    git fetch --quiet --no-tags --force origin "+refs/tags/$tag:refs/tags/$tag" \
        || _release_steps__die "refs/tags/$tag did not fetch from origin"
    _release_steps__resolve_tag_object "$PWD" "$tag"
    tag_object="$_RELEASE_STEPS__TAG_OBJECT"
    commit="$(git rev-parse "$tag_object^{commit}")"
    [[ "$commit" == "$event_commit" && "$commit" == "$(git rev-parse HEAD)" ]] \
        || _release_steps__die "$tag peels to $commit; the event's commit is $event_commit and the checkout is $(git rev-parse HEAD)"
    _release_steps__verify_tag_signature "$PWD" "$tag" "$tag_object" "$TAG_SIGNER_PRIMARY_FINGERPRINTS" \
        "$TAG_SIGNER_KEY_URL"
    echo "$tag verified: tag object $tag_object, commit $commit, signed by $_RELEASE_STEPS__TAG_PRIMARY"
    if [[ -n "${GITHUB_ENV:-}" ]]; then
        printf 'RELEASE_COMMIT=%s\nRELEASE_TAG_OBJECT=%s\n' "$commit" "$tag_object" >> "$GITHUB_ENV"
    fi
}

_release_steps__run_preflight() {
    local response dist
    _release_steps__require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
    # sign-rpms.sh reads the passphrase as the first stdin line, so a second line would corrupt the key read after it.
    [[ "$GPG_SIGNING_PASSPHRASE" != *$'\n'* ]] || _release_steps__die "GPG_SIGNING_PASSPHRASE spans more than one line"
    _release_steps__require_swap_off
    _release_steps__import_signing_key
    echo "imported signing key: primary $signing_primary"
    [[ "$signing_primary" == "$ARTIFACT_SIGNER_PRIMARY_FINGERPRINT" ]] \
        || _release_steps__die "GPG_SIGNING_KEY is key $signing_primary, not the pinned artifact signer $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT; a different primary is a different key, not a rotation"
    if [[ "${DISPATCH_RPM:-false}" == true ]]; then
        _release_steps__require_env RPM_REPO_DISPATCH_TOKEN
        response="$(GH_TOKEN="$RPM_REPO_DISPATCH_TOKEN" gh api repos/dag-node/rpm --jq .full_name 2>&1)" \
            || _release_steps__die "RPM_REPO_DISPATCH_TOKEN cannot read dag-node/rpm: $response"
        echo "the dispatch token reads $response"
    fi
    # Every image is pulled before sign-files makes the first signature, and sign-rpm runs these copies alone.
    _release_steps__ensure_podman
    for dist in "${!DIST_IMAGES[@]}"; do
        response="$(sudo podman pull --quiet "${DIST_IMAGES[$dist]}" 2>&1)" \
            || _release_steps__die "podman did not pull the $dist image ${DIST_IMAGES[$dist]} ($response); \`check-images\` names a digest the registry no longer serves, and the digest to pin"
        echo "pulled the $dist image ${DIST_IMAGES[$dist]}"
    done
}

# _release_steps__export_artifact_signer <file>: write the pinned primary's public key, from the imported
# secret, to <file>.
_release_steps__export_artifact_signer() {
    gpg --batch --armor --export "$ARTIFACT_SIGNER_PRIMARY_FINGERPRINT" > "$1"
    [[ -s "$1" ]] || _release_steps__die "the imported key does not hold the primary $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT"
}

_release_steps__sign_files() {
    [[ "$#" -ge 1 ]] || _release_steps__die "usage: sign-files <file>..."
    local file status primary
    _release_steps__require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
    _release_steps__require_swap_off
    _release_steps__import_signing_key
    for file in "$@"; do
        [[ -f "$file" ]] || _release_steps__die "$file is not a file"
        printf '%s' "$GPG_SIGNING_PASSPHRASE" | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 \
            --armor --detach-sign --output "$file.asc" "$file" \
            || _release_steps__die "gpg did not sign $file"
        status="$(gpg --batch --status-fd 1 --verify "$file.asc" "$file" 2>/dev/null)" || _release_steps__die "$file.asc does not verify"
        primary="$(printf '%s\n' "$status" | awk '/^\[GNUPG:\] VALIDSIG/ { print $NF; exit }')"
        [[ "$primary" == "$ARTIFACT_SIGNER_PRIMARY_FINGERPRINT" ]] \
            || _release_steps__die "$file.asc is signed by primary ${primary:-none}, not $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT"
        echo "$file.asc verified: signed by $primary"
    done
}

_release_steps__build_rpm() {
    [[ "$#" -ge 2 ]] || _release_steps__die "usage: build-rpm <output-directory> <render-nfpm.py arguments>..."
    local output="$1" download tarball nfpm dist config rpms
    shift
    _release_steps__require_env SOURCE_DATE_EPOCH
    [[ "$(uname -m)" == x86_64 ]] || _release_steps__die "the pinned nFPM checksum is for Linux x86_64, not $(uname -m)"
    mkdir -p "$output"
    compgen -G "$output/*.rpm" >/dev/null && _release_steps__die "$output already holds an RPM; build-rpm writes into an empty directory"
    _release_steps__create_scratch
    download="$scratch"
    tarball="nfpm_${NFPM_VERSION}_Linux_x86_64.tar.gz"
    "${CURL[@]}" -L -o "$download/$tarball" "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/$tarball" \
        || _release_steps__die "nFPM $NFPM_VERSION did not download"
    echo "$NFPM_TARBALL_SHA256  $download/$tarball" | sha256sum --check --quiet \
        || _release_steps__die "$tarball does not match the pinned SHA-256 $NFPM_TARBALL_SHA256"
    tar -xzf "$download/$tarball" -C "$download" || _release_steps__die "$tarball did not extract"
    nfpm="$download/nfpm"
    [[ -f "$nfpm" ]] || _release_steps__die "$tarball holds no nfpm binary at its top level"
    for dist in "${!DIST_IMAGES[@]}"; do
        config="$download/nfpm-$dist.yaml"
        python3 "$PACKAGING/render-nfpm.py" "$@" --dist "$dist" --output "$config" \
            || _release_steps__die "render-nfpm.py did not write the $dist configuration"
        "$nfpm" package --config "$config" --packager rpm --target "$output/" </dev/null \
            || _release_steps__die "nFPM did not build the $dist RPM from $config"
        rpms=("$output"/*."$dist".noarch.rpm)
        [[ "${#rpms[@]}" -eq 1 && -f "${rpms[0]}" ]] \
            || _release_steps__die "nFPM wrote ${#rpms[@]} files matching $output/*.$dist.noarch.rpm, not one"
        echo "built ${rpms[0]} with nFPM $NFPM_VERSION, SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
    done
    rpms=("$output"/*.rpm)
    [[ "${#rpms[@]}" -eq "${#DIST_IMAGES[@]}" ]] \
        || _release_steps__die "$output holds ${#rpms[@]} RPMs, not one for each of ${!DIST_IMAGES[*]}"
}

# _release_steps__resolve_dist_image <rpm-file-name>: set `image` to the image of the distribution the file
# name's dist tag names.
_release_steps__resolve_dist_image() {
    [[ "$1" =~ \.([a-z]+[0-9]+)\.noarch\.rpm$ && -n "${DIST_IMAGES[${BASH_REMATCH[1]}]:-}" ]] \
        || _release_steps__die "$1 does not end in .<dist>.noarch.rpm with a dist tag of ${!DIST_IMAGES[*]}"
    image="${DIST_IMAGES[${BASH_REMATCH[1]}]}"
}

# _release_steps__parse_image_pins <script-text>: set _RELEASE_STEPS__PIN_DISTS to the dist tags of
# the text's DIST_IMAGES declaration, in its order, and _RELEASE_STEPS__PIN_REFERENCES to the reference each
# one's <DIST>_IMAGE line pins. The pins are read as text, so the pins of another commit are checked without running
# that commit's code.
_release_steps__parse_image_pins() {
    local text="$1" declaration variable line dist
    _RELEASE_STEPS__PIN_DISTS=()
    _RELEASE_STEPS__PIN_REFERENCES=()
    declaration="$(grep -E '^declare -A DIST_IMAGES=\(' <<<"$text")" \
        || _release_steps__die "the release steps hold no \`declare -A DIST_IMAGES=(…)\` line"
    while [[ "$declaration" =~ \[([a-z]+[0-9]+)\]=\"\$([A-Z][A-Z0-9]*_IMAGE)\"(.*)$ ]]; do
        dist="${BASH_REMATCH[1]}" variable="${BASH_REMATCH[2]}" declaration="${BASH_REMATCH[3]}"
        [[ "$variable" == "${dist^^}_IMAGE" ]] \
            || _release_steps__die "DIST_IMAGES maps $dist to \$$variable, not \$${dist^^}_IMAGE"
        line="$(grep -E "^$variable=" <<<"$text")" \
            || _release_steps__die "the release steps hold no $variable= line"
        [[ "$line" =~ ^$variable=([^[:space:]]+)([[:space:]]+#.*)?$ ]] \
            || _release_steps__die "the $variable line is not \`$variable=<reference>\`, with an optional comment: $line"
        _RELEASE_STEPS__PIN_DISTS+=("$dist")
        _RELEASE_STEPS__PIN_REFERENCES[$dist]="${BASH_REMATCH[1]}"
    done
    [[ "${#_RELEASE_STEPS__PIN_DISTS[@]}" -gt 0 ]] \
        || _release_steps__die "the DIST_IMAGES declaration names no image: $declaration"
}

# skopeo from PATH, or on a GitHub runner installed with apt-get, as ensure_podman installs podman.
_release_steps__ensure_skopeo() {
    command -v skopeo >/dev/null && return 0
    if [[ "${GITHUB_ACTIONS:-}" == true ]] && command -v apt-get >/dev/null; then
        { sudo apt-get update -q && sudo apt-get install -y -q skopeo; } >&2 || true
    fi
    command -v skopeo >/dev/null \
        || _release_steps__die "\`skopeo\` is not installed and did not install; install it, then run again"
}

# _release_steps__read_manifest_digest <reference>: set _RELEASE_STEPS__SERVED_DIGEST to the SHA-256
# digest of the manifest the registry returns for <reference>, hashed from the raw bytes, which are the bytes a digest
# names. On a failure it returns 1 with skopeo's or sha256sum's message, on one line, in _RELEASE_STEPS__ANSWER. It is
# called as an `if` condition, where `set -e` is off, so each command's status is read here, and a hash that is not
# 64 hex digits is a failure: a pin and the refresh's replacement take the digest this function sets.
_release_steps__read_manifest_digest() {
    local manifest="$_RELEASE_STEPS__TEMP_DIR/manifest" answer hash
    _RELEASE_STEPS__SERVED_DIGEST=""
    if answer="$(skopeo inspect --raw "docker://$1" 2>&1 >"$manifest")"; then
        if ! hash="$(sha256sum <"$manifest" 2>&1)"; then
            _RELEASE_STEPS__ANSWER="sha256sum did not hash the manifest of $1 ($(_release_steps__format_one_line "$hash"))"
            return 1
        fi
        hash="${hash%% *}"
        if [[ ! "$hash" =~ ^[0-9a-f]{64}$ ]]; then
            _RELEASE_STEPS__ANSWER="sha256sum answered \`$(_release_steps__format_one_line "$hash")\` for the manifest of $1, not a SHA-256 digest"
            return 1
        fi
        _RELEASE_STEPS__SERVED_DIGEST="sha256:$hash"
        return 0
    fi
    answer="${answer//$'\n'/ }"
    _RELEASE_STEPS__ANSWER="${answer:-skopeo exited non-zero and printed nothing}"
    return 1
}

# Ask the registry of every reference in _RELEASE_STEPS__PIN_REFERENCES for the manifest at its pinned digest,
# without pulling a layer, and print one line per image. A manifest the registry reports unknown is an expiry, and its
# line names the digest the tag names now; any other failure is a registry that could not be read, and its line does
# not name a replacement. Sets _RELEASE_STEPS__EXPIRED_DISTS, _RELEASE_STEPS__CURRENT_DIGESTS (dist
# tag -> digest) and _RELEASE_STEPS__FAILED_COUNT.
_release_steps__check_pins() {
    local dist variable reference repository tag digest served
    _RELEASE_STEPS__EXPIRED_DISTS=()
    _RELEASE_STEPS__CURRENT_DIGESTS=()
    _RELEASE_STEPS__FAILED_COUNT=0
    _release_steps__ensure_skopeo
    _release_steps__create_temp_dir
    for dist in "${_RELEASE_STEPS__PIN_DISTS[@]}"; do
        variable="${dist^^}_IMAGE" reference="${_RELEASE_STEPS__PIN_REFERENCES[$dist]}"
        if [[ ! "$reference" =~ $_RELEASE_STEPS__IMAGE_REFERENCE_PATTERN ]]; then
            if [[ "$reference" == *@* ]]; then
                echo "::error::release-steps: $dist: $variable=$reference is not <repository>:<tag>@sha256:<digest>" >&2
            else
                echo "::error::release-steps: $dist: $variable=$reference carries no digest; every image is pinned as <repository>:<tag>@sha256:<digest>" >&2
            fi
            _RELEASE_STEPS__FAILED_COUNT=$((_RELEASE_STEPS__FAILED_COUNT + 1))
            continue
        fi
        repository="${BASH_REMATCH[1]}" tag="${BASH_REMATCH[3]}" digest="${BASH_REMATCH[4]}"
        if _release_steps__read_manifest_digest "$repository@$digest"; then
            served="$_RELEASE_STEPS__SERVED_DIGEST"
            if [[ "$served" == "$digest" ]]; then
                echo "release-steps: ok: $dist $repository:$tag serves the pinned digest ${digest:0:19}"
                continue
            fi
            _RELEASE_STEPS__ANSWER="the registry returned a manifest whose digest is $served"
        elif [[ "$_RELEASE_STEPS__ANSWER" =~ $_RELEASE_STEPS__MANIFEST_UNKNOWN_PATTERN ]] \
                && _release_steps__read_manifest_digest "$repository:$tag"; then
            served="$_RELEASE_STEPS__SERVED_DIGEST"
            echo "::error::release-steps: $dist: $repository:$tag no longer serves the pinned digest $digest (manifest unknown); the tag names $served now, so the pin to paste is $variable=$repository:$tag@$served" >&2
            _RELEASE_STEPS__EXPIRED_DISTS+=("$dist")
            _RELEASE_STEPS__CURRENT_DIGESTS[$dist]="$served"
            _RELEASE_STEPS__FAILED_COUNT=$((_RELEASE_STEPS__FAILED_COUNT + 1))
            continue
        fi
        echo "::error::release-steps: $dist: the registry could not be read for $repository:$tag ($_RELEASE_STEPS__ANSWER); this is an outage, not an expiry: retry later, nothing was changed" >&2
        _RELEASE_STEPS__FAILED_COUNT=$((_RELEASE_STEPS__FAILED_COUNT + 1))
    done
}

# _release_steps__read_caller_pins <directory>: set the _RELEASE_STEPS__CALLER_* arrays from every
# `uses:` line of <directory>/.github/workflows/*.yml and *.yaml that names a workflow of the tools repository, one
# element per line. A line naming that repository's workflows in any other form, quoted or in another case, is refused,
# so a textual reading cannot pass over a pin.
_release_steps__read_caller_pins() {
    local directory="$1" file number line pattern
    local repository="${_RELEASE_STEPS__TOOLS_REPOSITORY//./\\.}"
    pattern="^([[:space:]]*(-[[:space:]]+)?uses:[[:space:]]+)$repository/\.github/workflows/([A-Za-z0-9._-]+)@([^[:space:]#]+)([[:space:]]+#[[:space:]]*(.*))?$"
    _RELEASE_STEPS__CALLER_FILES=()
    _RELEASE_STEPS__CALLER_LINE_NUMBERS=()
    _RELEASE_STEPS__CALLER_PREFIXES=()
    _RELEASE_STEPS__CALLER_WORKFLOWS=()
    _RELEASE_STEPS__CALLER_REFS=()
    _RELEASE_STEPS__CALLER_COMMENTS=()
    for file in "$directory"/.github/workflows/*.yml "$directory"/.github/workflows/*.yaml; do
        [[ -f "$file" ]] || continue
        number=0
        while IFS= read -r line || [[ -n "$line" ]]; do
            number=$((number + 1))
            [[ "${line,,}" == *"${_RELEASE_STEPS__TOOLS_REPOSITORY,,}/.github/workflows/"* ]] || continue
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ "$line" =~ $pattern ]] \
                || _release_steps__die "${file#"$directory"/}:$number names a workflow of $_RELEASE_STEPS__TOOLS_REPOSITORY in a form other than \`uses: $_RELEASE_STEPS__TOOLS_REPOSITORY/.github/workflows/<file>@<ref> # <comment>\`: $line"
            _RELEASE_STEPS__CALLER_FILES+=("${file#"$directory"/}")
            _RELEASE_STEPS__CALLER_LINE_NUMBERS+=("$number")
            _RELEASE_STEPS__CALLER_PREFIXES+=("${BASH_REMATCH[1]}")
            _RELEASE_STEPS__CALLER_WORKFLOWS+=("${BASH_REMATCH[3]}")
            _RELEASE_STEPS__CALLER_REFS+=("${BASH_REMATCH[4]}")
            _RELEASE_STEPS__CALLER_COMMENTS+=("${BASH_REMATCH[6]}")
        done <"$file"
    done
}

# Refuse when the caller's `uses:` lines, read by read_caller_pins, do not all carry one `@<ref>`: validate checks
# the image pins of the commit its own line names, which are the ones a release uses only while the lines agree.
_release_steps__require_equal_caller_pins() {
    local index pins="" equal=true
    for index in "${!_RELEASE_STEPS__CALLER_REFS[@]}"; do
        pins+="${pins:+, }${_RELEASE_STEPS__CALLER_FILES[$index]}: @${_RELEASE_STEPS__CALLER_REFS[$index]}"
        [[ "${_RELEASE_STEPS__CALLER_REFS[$index]}" == "${_RELEASE_STEPS__CALLER_REFS[0]}" ]] || equal=false
    done
    [[ "$equal" == true ]] \
        || _release_steps__die "the \`uses:\` lines pin different refs ($pins); make them one, then run again"
}

# check-images over this file's own pins, after proving that the text reader returns the DIST_IMAGES this shell holds,
# since the same reader reads the pins of another commit. Run in a caller's checkout, it first holds the caller's
# `uses:` lines to one ref, and a failed image check adds where the refresh may already be.
_release_steps__check_images() {
    [[ "$#" -eq 0 ]] || _release_steps__die "usage: check-images"
    local dist count callers
    _release_steps__read_caller_pins "$PWD"
    callers="${#_RELEASE_STEPS__CALLER_REFS[@]}"
    if [[ "$callers" -eq 0 ]]; then
        echo "release-steps: ok: no \`uses:\` line under .github/workflows/ names a workflow of $_RELEASE_STEPS__TOOLS_REPOSITORY; no pins to compare"
    else
        _release_steps__require_equal_caller_pins
        echo "release-steps: ok: the $callers \`uses:\` lines naming $_RELEASE_STEPS__TOOLS_REPOSITORY pin @${_RELEASE_STEPS__CALLER_REFS[0]}"
        [[ " ${_RELEASE_STEPS__CALLER_WORKFLOWS[*]} " == *" set-release.yml "* ]] \
            || echo "release-steps: ok: no \`uses:\` line names set-release.yml, so a release does not run through these pins"
    fi
    _release_steps__parse_image_pins "$(<"$PACKAGING/release-steps.sh")"
    count="${#_RELEASE_STEPS__PIN_DISTS[@]}"
    [[ "$count" -eq "${#DIST_IMAGES[@]}" ]] \
        || _release_steps__die "the text of the release steps pins ${_RELEASE_STEPS__PIN_DISTS[*]}, not ${!DIST_IMAGES[*]}"
    for dist in "${_RELEASE_STEPS__PIN_DISTS[@]}"; do
        [[ "${_RELEASE_STEPS__PIN_REFERENCES[$dist]}" == "${DIST_IMAGES[$dist]:-}" ]] \
            || _release_steps__die "the text of the release steps pins $dist at ${_RELEASE_STEPS__PIN_REFERENCES[$dist]}, not ${DIST_IMAGES[$dist]:-absent}"
    done
    echo "release-steps: check-images: checking $count images"
    _release_steps__check_pins
    if [[ "$callers" -gt 0 && "${#_RELEASE_STEPS__EXPIRED_DISTS[@]}" -gt 0 ]]; then
        echo "::error::release-steps: $_RELEASE_STEPS__TOOLS_REPOSITORY's develop may already carry the refresh: in its checkout, \`git log origin/develop -- packaging/release-steps.sh\`; then \`bash <ai-tools-assets-tools>/packaging/release-steps.sh repin-tools --id <id> <sha>\` here" >&2
    fi
    [[ "$_RELEASE_STEPS__FAILED_COUNT" -eq 0 ]] \
        || _release_steps__die "check-images: $_RELEASE_STEPS__FAILED_COUNT of $count images failed the check"
    echo "release-steps: ok: every image resolves"
}

# podman and runc from PATH, or installed with apt-get where the runner image lacks them; ubuntu-26.04 ships podman
# without runc.
_release_steps__ensure_podman() {
    local command missing=()
    for command in podman runc; do
        command -v "$command" >/dev/null || missing+=("$command")
    done
    if [[ "${#missing[@]}" -gt 0 ]]; then
        { sudo apt-get update -q && sudo apt-get install -y -q "${missing[@]}"; } \
            || _release_steps__die "${missing[*]} not installed and did not install"
    fi
    sudo mkdir -p /etc/containers/containers.conf.d
    printf '[engine]\nruntime = "runc"\n' | sudo tee /etc/containers/containers.conf.d/10-runc.conf >/dev/null
    sudo podman info --format 'podman runtime: {{.Host.OCIRuntime.Name}} {{.Host.OCIRuntime.Version}}'
}

# Print `python3 --version` and refuse unless it names Python 3.9, the floor: the suite proves the floor only while
# the interpreter it runs on is that release. test-floor runs it inside the container.
_release_steps__require_floor_python() {
    local version
    version="$(python3 --version 2>&1)" \
        || _release_steps__die "\`python3 --version\` failed ($(_release_steps__format_one_line "$version")); the floor suite does not run"
    echo "$version"
    [[ "$version" == "Python 3.9."* ]] \
        || _release_steps__die "python3 is $(_release_steps__format_one_line "$version"), not Python 3.9; the floor suite runs only on the floor's interpreter"
}

_release_steps__find_tests() {
    [[ "$#" -eq 0 ]] || _release_steps__die "usage: find-tests"
    local directory match found=()
    shopt -s nullglob
    for directory in tests sets/*/skills/*/tests; do
        [[ -d "$directory" ]] || continue
        _release_steps__read_output match find "$directory" -type f -name 'test*.py' -print -quit \
            || _release_steps__die "\`find $directory\` failed ($_RELEASE_STEPS__ANSWER), so the test directories are unknown"
        [[ -z "$match" ]] || found+=("$directory")
    done
    shopt -u nullglob
    if [[ "${#found[@]}" -eq 0 ]]; then
        echo "::notice::no tests/ at the root or sets/*/skills/*/tests holds a test*.py; the suite does not run" >&2
        return 0
    fi
    printf '%s\n' "${found[@]}"
}

_release_steps__test_floor() {
    [[ "$#" -ge 1 ]] || _release_steps__die "usage: test-floor <directory>..."
    local directory
    for directory in "$@"; do
        [[ "$directory" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ && "/$directory/" != */../* && -d "$directory" ]] \
            || _release_steps__die "$directory is not a directory under $PWD, named by a relative path"
    done
    _release_steps__ensure_podman
    # The functions the container runs are this file's, written into its script, so the check there is the one the suite
    # holds.
    sudo podman run --rm --pull=missing --entrypoint /usr/bin/bash -v "$PWD:/src:ro" -v "$PACKAGING:/packaging:ro" \
        -w /src -e PYTHONDONTWRITEBYTECODE=1 "$EL9_IMAGE" -c "set -euo pipefail
_RELEASE_STEPS__DIE_PREFIX='' _RELEASE_STEPS__DIE_SUFFIX=''
$(declare -f _release_steps__die _release_steps__format_one_line _release_steps__require_floor_python)"'
uid="$1" gid="$2"
shift 2
bash /packaging/el-repos.sh
dnf -y -q --setopt=install_weak_deps=False install git
_release_steps__require_floor_python
home="$(mktemp -d)"
chown "$uid:$gid" "$home"
drop=(setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs --)
HOME="$home" "${drop[@]}" grep -x "NoNewPrivs:[[:space:]]*1" /proc/self/status \
    || _release_steps__die "the suites would run without no_new_privs under ${drop[*]}"
failed=()
for directory in "$@"; do
    HOME="$home" "${drop[@]}" python3 -m unittest discover -s "$directory" -v || failed+=("$directory")
done
[[ "${#failed[@]}" -eq 0 ]] || _release_steps__die "the suite failed at the floor in ${failed[*]}"
echo "the suite passed at the floor in $*"' _ "$(id -u)" "$(id -g)" "$@" </dev/null \
        || _release_steps__die "the floor suite did not pass in $EL9_IMAGE"
}

# _release_steps__print_mounts: one line per mount of a /proc/self/mounts listing on stdin, with its first option
# (`ro` or `rw`) and its type. The verify job's own mounts (`/`, `/in`, `/rpmdb`) are labelled `job`, and the ones
# podman adds (`/etc/hosts`, `/run/.containerenv`, the masked paths) `podman`, so the log states each one's access.
# The verify containers run it, written into their script.
_release_steps__print_mounts() {
    local mountpoint type options origin
    while read -r _ mountpoint type options _; do
        case "$mountpoint" in
            / | /in | /rpmdb) origin=job ;;
            *) origin=podman ;;
        esac
        echo "$origin mount: ${options%%,*} $mountpoint ($type)"
    done
}

_release_steps__sign_rpm() {
    [[ "$#" -ge 1 ]] || _release_steps__die "usage: sign-rpm <rpm>..."
    local rpm name names=() work verify out image
    _release_steps__require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
    _release_steps__require_swap_off
    for rpm in "$@"; do
        [[ -f "$rpm" ]] || _release_steps__die "$rpm is not a file"
        _release_steps__resolve_dist_image "$(basename "$rpm")"
    done
    _release_steps__ensure_podman
    _release_steps__create_scratch
    work="$scratch"
    for rpm in "$@"; do
        name="$(basename "$rpm")"
        [[ -e "$work/$name" ]] && _release_steps__die "two RPMs are named $name"
        cp "$rpm" "$work/$name"
        names+=("$name")
    done
    cp "$PACKAGING/sign-rpms.sh" "$PACKAGING/el-repos.sh" "$work/"

    container="ai-tools-assets-sign-tools-$$"
    sudo podman run --pull=never --name "$container" --entrypoint /usr/bin/bash -v "$work:/in:ro" "$EL9_IMAGE" -c '
            set -euo pipefail
            bash /in/el-repos.sh
            dnf -y -q --setopt=install_weak_deps=False install rpm-sign gnupg2' </dev/null \
        || _release_steps__die "rpm-sign and gnupg2 did not install in $EL9_IMAGE"
    sudo podman commit --quiet "$container" "$SIGN_IMAGE" >/dev/null
    sudo podman rm "$container" >/dev/null

    # The key enters this container alone, and it has no network.
    container="ai-tools-assets-sign-$$"
    out="$(printf '%s\n%s\n' "$GPG_SIGNING_PASSPHRASE" "$GPG_SIGNING_KEY" \
        | sudo podman run -i --pull=never --name "$container" --network=none --shm-size=16m --entrypoint /usr/bin/bash \
            -v "$work:/in:ro" "$SIGN_IMAGE" -c '
            set -euo pipefail
            mkdir /out
            for name in "$@"; do cp "/in/$name" /out/; done
            bash /in/sign-rpms.sh --secrets-stdin /out/sign-rpms.pub "${@/#//out/}"' _ "${names[@]}")" \
        || _release_steps__die "sign-rpms.sh failed in $SIGN_IMAGE: $out"
    printf '%s\n' "$out"
    grep -q 'signed and verified' <<<"$out" || _release_steps__die "sign-rpms.sh did not run in $SIGN_IMAGE"
    for name in "${names[@]}"; do
        sudo podman cp "$container:/out/$name" "$work/$name"
        sudo chown "$(id -u):$(id -g)" "$work/$name"
    done
    sudo podman rm "$container" >/dev/null
    container=""

    _release_steps__import_signing_key
    _release_steps__create_scratch
    verify="$scratch"
    _release_steps__export_artifact_signer "$verify/artifact-signer.asc"
    for name in "${names[@]}"; do cp "$work/$name" "$verify/$name"; done
    chmod 0644 "$verify"/*
    chmod 0755 "$verify"
    for name in "${names[@]}"; do
        _release_steps__resolve_dist_image "$name"
        # An option list after `--tmpfs` replaces podman's defaults, so `rw,nosuid,nodev,noexec` are restated beside
        # the size.
        out="$(sudo podman run --rm --pull=never --network=none --ipc=none --cap-drop=all \
                --security-opt=no-new-privileges --read-only --read-only-tmpfs=false --pids-limit=64 \
                --tmpfs /rpmdb:rw,nosuid,nodev,noexec,size=32m,mode=0700 --entrypoint /usr/bin/bash -v "$verify:/in:ro" \
                "$image" -c "$(declare -f _release_steps__print_mounts)"'
                _release_steps__print_mounts </proc/self/mounts
                rpmkeys --dbpath /rpmdb --import /in/artifact-signer.asc || exit 1
                rpm --version
                rpmkeys --dbpath /rpmdb -Kv "/in/$1" 2>&1' _ "$name" 2>&1)" \
            || _release_steps__die "rpmkeys -Kv refused $name in $image: $out"
        printf '%s:\n%s\n' "$image" "$out"
        grep -Eqi "$SIGNATURE_OK_PATTERN" <<<"$out" \
            || _release_steps__die "$name carries no signature that verifies in $image against $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT"
    done
    for rpm in "$@"; do
        cp "$work/$(basename "$rpm")" "$rpm"
        echo "$rpm signed in $EL9_IMAGE and verified in the image of its distribution"
    done
}

_release_steps__create_release() {
    [[ "$#" -ge 3 ]] || _release_steps__die "usage: create-release <tag> <notes> <file>..."
    local tag="$1" notes="$2" remote flags=()
    shift 2
    _release_steps__require_env GH_TOKEN RELEASE_TAG_OBJECT
    remote="$(git ls-remote origin "refs/tags/$tag" | awk -v ref="refs/tags/$tag" '$2 == ref { print $1 }')"
    [[ "$remote" == "$RELEASE_TAG_OBJECT" ]] \
        || _release_steps__die "refs/tags/$tag on the remote is ${remote:-absent}; the verified tag object is $RELEASE_TAG_OBJECT, so the tag moved and the release is not created"
    # The version is the part after the last slash (v1.2.3, or <set>/v1.2.3), so a hyphen in a set name is not read
    # as a prerelease.
    [[ "${tag##*/}" == *-* ]] && flags+=(--prerelease)
    gh release create "$tag" --verify-tag --title "$tag" --notes "$notes" "${flags[@]}" "$@"
}

_release_steps__dispatch() {
    [[ "$#" -eq 1 ]] || _release_steps__die "usage: dispatch <tag>"
    local tag="$1" attempts=5 attempt
    _release_steps__require_env RPM_REPO_DISPATCH_TOKEN GITHUB_REPOSITORY
    # preflight read dag-node/rpm with this token, so a failure here is transient and is retried with a backoff.
    for attempt in $(seq 1 "$attempts"); do
        # -f sends each value as the string it is; -F would read a value starting with @ as a file name.
        if GH_TOKEN="$RPM_REPO_DISPATCH_TOKEN" gh api repos/dag-node/rpm/dispatches \
                -f event_type=publish-rpm \
                -f "client_payload[project]=$GITHUB_REPOSITORY" \
                -f "client_payload[tag]=$tag"; then
            echo "dispatched publish-rpm for $GITHUB_REPOSITORY $tag to dag-node/rpm"
            return 0
        fi
        [[ "$attempt" -lt "$attempts" ]] && sleep $((attempt * 10))
    done
    _release_steps__die "the dispatch to dag-node/rpm failed after $attempts attempts; the release exists, and re-running this step dispatches again"
}

# The digest refresh. refresh-images (in a checkout of the tools) and repin-tools (in a caller's checkout, run from
# the tools' tree) each turn one half of a refresh into a branch, a signed-off commit and a pull request, with a body
# generated from what the command read. Each reads its progress from the repositories and does not keep a state file:
# a branch already carrying the generated change is reused, a pushed branch is not pushed again, and an open pull
# request is printed instead of created, so the same command line continues after an interruption. Every local step
# precedes the push and the pull request comes last. Each step prints a line before it acts and an `ok:` line once it
# verified the result. A dry run fetches origin/develop and writes the change in the scratch worktree, which is removed
# on exit; it does not commit, push, open a pull request, or change a branch, a tag or the working tree.
_RELEASE_STEPS__COMMAND=""
_RELEASE_STEPS__ID=""
_RELEASE_STEPS__DRY_RUN=false
_RELEASE_STEPS__GH_READY=false
_RELEASE_STEPS__CALLERS=()
_RELEASE_STEPS__PULL_REQUEST=""
_RELEASE_STEPS__TARGET=""
_RELEASE_STEPS__GITHUB_REPOSITORY=""
_RELEASE_STEPS__WAITED_SECONDS=0
_RELEASE_STEPS__PULL_REQUEST_URL=""
# What repin-tools read: the tag signers the tools' release.yml pins, a refresh commit's dists and date, and
# a release's commit, version and changelog headlines.
_RELEASE_STEPS__TAG_SIGNERS=""
_RELEASE_STEPS__TAG_SIGNER_KEY_URL=""
_RELEASE_STEPS__REFRESH_DISTS=()
declare -A _RELEASE_STEPS__REFRESH_DATES=()
_RELEASE_STEPS__CALLER_PIN_COMMIT=""
_RELEASE_STEPS__RELEASE_COMMIT=""
_RELEASE_STEPS__RELEASE_VERSION=""
_RELEASE_STEPS__RELEASE_HEADLINES=""
# GitHub serves a pushed branch, a merged commit's pull request and a release lookup eventually; a lookup is retried
# this many times, waiting attempt x this many seconds after each failure but the last, as dispatch retries.
_RELEASE_STEPS__RETRY_ATTEMPTS=5
_RELEASE_STEPS__RETRY_SECONDS=10
# gh pr create's answers while GitHub does not yet serve a branch just pushed.
_RELEASE_STEPS__BRANCH_NOT_SERVED_PATTERN="not found|could not find|Head sha can't be blank|No commits between|must first push"
_RELEASE_STEPS__TOOLS_URL="https://github.com/$_RELEASE_STEPS__TOOLS_REPOSITORY.git"

_release_steps__step() { echo "release-steps: $_RELEASE_STEPS__COMMAND: $*"; }
_release_steps__ok() { echo "release-steps: ok: $*"; }
_release_steps__next() { echo "release-steps: next, $*"; }
_release_steps__warn() { echo "::warning::release-steps: $*" >&2; }

_release_steps__print_change_usage() {
    cat <<'USAGE'
usage: release-steps.sh refresh-images --id <ID> [--caller <path>]... [--dry-run]
       release-steps.sh repin-tools --id <ID> <sha>|<vX.Y.Z> [--pr <number>] [--dry-run]

refresh-images, in a checkout of ai-tools-assets-tools on a clean develop equal to origin/develop: runs check-images,
rewrites each expired <DIST>_IMAGE pin to the digest its tag resolves to now, adds the CHANGELOG.md entry, and opens
the pull request build/<ID>-<dists>-digest into develop. --caller runs repin-tools' preconditions in each named
caller's checkout first, so a caller that is not ready refuses the refresh before this tree changes.

repin-tools, in a caller's checkout on a clean develop equal to origin/develop, run from the tools' tree
(bash ../ai-tools-assets-tools/packaging/release-steps.sh repin-tools ...): rewrites every `uses:` line naming a
workflow of dag-node/ai-tools-assets-tools to one commit and opens the pull request into develop.
  <sha>     a digest refresh merged into the tools' develop, pinned until the next tools release; refused unless it
            changes image pin lines and CHANGELOG.md alone. --pr names its pull request where GitHub does not.
  <vX.Y.Z>  a tools release: a tag signed by a key the tools' release.yml pins, with a release whose run passed.

<ID> is <PREFIX>-<yyMMdd><letter>, as ATR-261103a, and names the branch. --dry-run runs every check, prints the diff,
the title and the body, and does not commit, push, open a pull request, or change a branch, a tag or the working tree.
Re-running a command after an interruption continues it.
USAGE
}

# _release_steps__parse_change_arguments <command> <argument>...: set the options of refresh-images or
# repin-tools, refusing one the command does not take.
_release_steps__parse_change_arguments() {
    local command="$1"
    shift
    _RELEASE_STEPS__COMMAND="$command"
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --help) _release_steps__print_change_usage; exit 0 ;;
            --dry-run) _RELEASE_STEPS__DRY_RUN=true ;;
            --id) [[ "$#" -ge 2 ]] || _release_steps__die "\`--id\` takes a value"
                _RELEASE_STEPS__ID="$2"; shift ;;
            --caller) [[ "$command" == refresh-images && "$#" -ge 2 ]] \
                    || _release_steps__die "\`--caller <path>\` is an option of refresh-images"
                _RELEASE_STEPS__CALLERS+=("$2"); shift ;;
            --pr) [[ "$command" == repin-tools && "$#" -ge 2 && "$2" =~ ^[1-9][0-9]*$ ]] \
                    || _release_steps__die "\`--pr <number>\` is an option of repin-tools"
                _RELEASE_STEPS__PULL_REQUEST="$2"; shift ;;
            -*) _release_steps__die "$command does not take $1; see \`release-steps.sh $command --help\`" ;;
            *) [[ "$command" == repin-tools && -z "$_RELEASE_STEPS__TARGET" ]] \
                    || _release_steps__die "$command does not take the argument $1; see \`release-steps.sh $command --help\`"
                _RELEASE_STEPS__TARGET="$1" ;;
        esac
        shift
    done
    [[ "$_RELEASE_STEPS__ID" =~ ^[A-Z][A-Z0-9]{0,15}-[0-9]{6}[a-z]$ ]] \
        || _release_steps__die "\`--id\` is required, as \`<PREFIX>-<yyMMdd><letter>\` (for example \`ATR-261103a\`)"
    [[ "$command" == refresh-images || -n "$_RELEASE_STEPS__TARGET" ]] \
        || _release_steps__die "give a commit SHA of the tools (a digest refresh) or a release tag \`vX.Y.Z\`"
}

# _release_steps__read_github_repository <directory>: set _RELEASE_STEPS__GITHUB_REPOSITORY to the
# <owner>/<name> of the GitHub repository the checkout's origin is. The configured URL is read as written, before
# any url.<base>.insteadOf rewrite.
_release_steps__read_github_repository() {
    local url
    url="$(git -C "$1" config --get remote.origin.url)" || _release_steps__die "$1 has no remote origin"
    url="${url%/}"
    url="${url%.git}"
    [[ "$url" =~ ^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$ ]] \
        || _release_steps__die "the origin of $1 is $url, not a repository on github.com"
    _RELEASE_STEPS__GITHUB_REPOSITORY="${BASH_REMATCH[2]}"
}

# _release_steps__read_toplevel <directory>: print the root of the work tree <directory> is in, resolved.
_release_steps__read_toplevel() {
    local toplevel
    toplevel="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || return 1
    realpath "$toplevel"
}

# Refuse unless the current directory is the root of the checkout of the tools that holds this script.
_release_steps__require_tools_checkout() {
    local here tools
    here="$(realpath "$PWD")"
    tools="$(_release_steps__read_toplevel "$PACKAGING")" || tools=""
    [[ -n "$tools" && "$(_release_steps__read_toplevel "$PWD")" == "$here" && "$here" == "$tools" ]] \
        || _release_steps__die "$PWD is not a checkout of ai-tools-assets-tools; run from the repository root"
    _release_steps__read_github_repository "$here"
    [[ "${_RELEASE_STEPS__GITHUB_REPOSITORY,,}" == "${_RELEASE_STEPS__TOOLS_REPOSITORY,,}" ]] \
        || _release_steps__die "$PWD is not a checkout of ai-tools-assets-tools; run from the repository root"
}

# _release_steps__require_caller_checkout <directory>: refuse unless <directory> is the root of a checkout
# of a repository other than the tools that holds a .github/workflows/ directory.
_release_steps__require_caller_checkout() {
    local directory="$1" here packaging
    # The tools' own checkout is the one whose packaging/ holds this script, a path comparison with no read to fail.
    packaging="$(realpath "$PACKAGING")" || _release_steps__die "the directory of this script, $PACKAGING, could not be resolved"
    here="$(realpath "$directory" 2>/dev/null)" || here=""
    [[ -n "$here" && "$(_release_steps__read_toplevel "$directory")" == "$here" \
        && -d "$here/.github/workflows" && "$here/packaging" != "$packaging" ]] \
        || _release_steps__die "$directory is not a checkout of a caller with a \`.github/workflows/\`; run from the repository root"
}

# Refuse unless this script runs from a checkout of the tools, whose tree repin-tools reads.
_release_steps__require_tools_tree() {
    local tools url
    tools="$(_release_steps__read_toplevel "$PACKAGING")" || tools=""
    url="$(git -C "${tools:-/}" config --get remote.origin.url 2>/dev/null)" || url=""
    url="${url%/}"
    url="${url%.git}"
    if [[ -z "$tools" || "${url,,}" != *[:/]"${_RELEASE_STEPS__TOOLS_REPOSITORY,,}" ]]; then
        _release_steps__die "run from ai-tools-assets-tools's tree, in the caller's checkout: \`bash <ai-tools-assets-tools>/packaging/release-steps.sh repin-tools …\`"
    fi
}

# Require gh logged in to github.com; a dry run continues without it and does not read GitHub.
_release_steps__require_gh() {
    if command -v gh >/dev/null && gh auth status --hostname github.com >/dev/null 2>&1; then
        _RELEASE_STEPS__GH_READY=true
        return 0
    fi
    [[ "$_RELEASE_STEPS__DRY_RUN" == true ]] \
        || _release_steps__die "\`gh\` is not logged in to github.com; run \`gh auth login\`"
    _release_steps__warn "\`gh\` is not logged in to github.com, so the dry run does not read GitHub"
}

# _release_steps__require_synced_develop <directory>: refuse unless the checkout is clean, on develop, and
# develop equals origin/develop after a fetch.
_release_steps__require_synced_develop() {
    local directory="$1" branch answer counts behind ahead changes
    _release_steps__read_output changes git -C "$directory" status --porcelain \
        || _release_steps__die "\`git status\` could not read the tree of $directory ($_RELEASE_STEPS__ANSWER); fix the checkout, then run again"
    if [[ -n "$changes" ]]; then
        git -C "$directory" status --short >&2
        _release_steps__die "the tree has uncommitted changes (\`git status --short\` printed above); commit or stash them, then run again"
    fi
    branch="$(git -C "$directory" symbolic-ref --quiet --short HEAD)" || branch="a detached HEAD"
    [[ "$branch" == develop ]] \
        || _release_steps__die "the checkout is on $branch, not \`develop\`; run \`git switch develop\`"
    _release_steps__step "fetching origin/develop in $directory"
    # `--no-tags`: the fetch updates origin/develop alone and does not add a tag to the checkout.
    answer="$(git -C "$directory" fetch --quiet --no-tags origin +refs/heads/develop:refs/remotes/origin/develop 2>&1)" \
        || _release_steps__die "\`git fetch origin develop\` failed in $directory (${answer//$'\n'/ }); check the network and the remote, then run again"
    _release_steps__read_output counts git -C "$directory" rev-list --left-right --count develop...origin/develop \
        || _release_steps__die "\`git rev-list\` could not compare develop with origin/develop in $directory ($_RELEASE_STEPS__ANSWER); run the same command again"
    [[ "$counts" =~ ^([0-9]+)[[:space:]]+([0-9]+)$ ]] \
        || _release_steps__die "\`git rev-list --count\` answered \`$(_release_steps__format_one_line "$counts")\` in $directory, not two counts; run the same command again"
    ahead="${BASH_REMATCH[1]}" behind="${BASH_REMATCH[2]}"
    if [[ "$behind" -gt 0 && "$ahead" -gt 0 ]]; then
        _release_steps__die "\`develop\` is $behind behind and $ahead ahead of \`origin/develop\` after a fetch; rebase the local commits on \`origin/develop\` first"
    elif [[ "$behind" -gt 0 ]]; then
        _release_steps__die "\`develop\` is $behind behind \`origin/develop\` after a fetch; run \`git pull --ff-only\` first"
    elif [[ "$ahead" -gt 0 ]]; then
        _release_steps__die "\`develop\` is $ahead ahead of \`origin/develop\` after a fetch; push or rebase the local commits first"
    fi
    _release_steps__ok "develop = origin/develop ($(git -C "$directory" rev-parse --short=7 develop))"
}

# _release_steps__read_output <variable> <command>...: run <command>, set <variable> to its stdout, and return its
# status. stdout and stderr go to separate files, so a diagnostic printed beside a successful answer does not reach
# <variable>. On a failure <variable> is empty and _RELEASE_STEPS__ANSWER holds the diagnostics and the output on one
# line, for the caller's refusal. `set -e` does not stop at a failed producer of a process substitution or of
# a command substitution inside `[[ ]]` or an argument, and a failed one can print part of its answer, so a caller
# reads the status before it reads <variable>.
_release_steps__read_output() {
    local captured_variable="$1" captured_status=0 captured_output
    shift
    _release_steps__create_temp_dir
    "$@" >"$_RELEASE_STEPS__TEMP_DIR/read-output" 2>"$_RELEASE_STEPS__TEMP_DIR/read-diagnostics" || captured_status=$?
    # Callers run this as `read_output … || die`, where `set -e` is off, so the read-back's own status is read here.
    # `cat`, not `$(<file)`: bash's form returns 0 with an empty answer when the read fails after the open succeeds.
    if ! captured_output="$(cat -- "$_RELEASE_STEPS__TEMP_DIR/read-output")"; then
        _RELEASE_STEPS__ANSWER="the output of \`$1\` could not be read back"
        printf -v "$captured_variable" '%s' ""
        return 1
    fi
    if [[ "$captured_status" -ne 0 ]]; then
        _RELEASE_STEPS__ANSWER="$(_release_steps__format_one_line "$(<"$_RELEASE_STEPS__TEMP_DIR/read-diagnostics")" \
            "$captured_output")"
        _RELEASE_STEPS__ANSWER="${_RELEASE_STEPS__ANSWER:-exit status $captured_status, no output}"
        printf -v "$captured_variable" '%s' ""
        return "$captured_status"
    fi
    printf -v "$captured_variable" '%s' "$captured_output"
}

# _release_steps__read_github <what> <ref> <definitive-pattern> <empty-ok> <command>...: run a GitHub lookup,
# retrying a failure, or an empty answer unless <empty-ok> is true, waiting attempt x _RELEASE_STEPS__RETRY_SECONDS
# between attempts. Returns 0 with the command's stdout alone in _RELEASE_STEPS__ANSWER, so a diagnostic gh prints
# beside a successful answer does not reach a file the answer is written into; 1 at once on a failure whose diagnostics
# match <definitive-pattern>; and 2 once the attempts are spent, with _RELEASE_STEPS__WAITED_SECONDS set. After
# a failure, _RELEASE_STEPS__ANSWER holds its diagnostics and output on one line.
_release_steps__read_github() {
    local what="$1" ref="$2" definitive="$3" empty_ok="$4" attempt answer diagnostics wait
    shift 4
    _release_steps__create_temp_dir
    diagnostics="$_RELEASE_STEPS__TEMP_DIR/diagnostics"
    _RELEASE_STEPS__WAITED_SECONDS=0
    for ((attempt = 1; attempt <= _RELEASE_STEPS__RETRY_ATTEMPTS; attempt++)); do
        if answer="$("$@" 2>"$diagnostics")"; then
            if [[ -n "$answer" || "$empty_ok" == true ]]; then
                _RELEASE_STEPS__ANSWER="$answer"
                return 0
            fi
            answer="an empty answer"
        else
            answer="$(_release_steps__format_one_line "$(<"$diagnostics")" "$answer")"
            if [[ -n "$definitive" && "$answer" =~ $definitive ]]; then
                _RELEASE_STEPS__ANSWER="$answer"
                return 1
            fi
        fi
        _RELEASE_STEPS__ANSWER="$answer"
        if [[ "$attempt" -lt "$_RELEASE_STEPS__RETRY_ATTEMPTS" ]]; then
            wait=$((attempt * _RELEASE_STEPS__RETRY_SECONDS))
            echo "release-steps: retry $attempt/$_RELEASE_STEPS__RETRY_ATTEMPTS: $what for $ref ($answer); waiting $wait s"
            sleep "$wait"
            _RELEASE_STEPS__WAITED_SECONDS=$((_RELEASE_STEPS__WAITED_SECONDS + wait))
        fi
    done
    return 2
}

# _release_steps__format_one_line <text>...: print the texts on one line, each run of whitespace one space.
_release_steps__format_one_line() {
    local words
    read -ra words <<<"${*//$'\n'/ }"
    printf '%s' "${words[*]}"
}

# _release_steps__die_after_retries <what> <ref>: the refusal once read_github spent its attempts.
_release_steps__die_after_retries() {
    _release_steps__die "$1 for $2 failed $_RELEASE_STEPS__RETRY_ATTEMPTS times over $_RELEASE_STEPS__WAITED_SECONDS s (last answer: $_RELEASE_STEPS__ANSWER); GitHub may still be catching up: run the same command again in a minute"
}

# _release_steps__list_open_pull_requests <github-repository> <note>: print each pull request open into
# develop, with <note>; a dry run without gh says it did not read them.
_release_steps__list_open_pull_requests() {
    local repository="$1" note="$2" status=0
    if [[ "$_RELEASE_STEPS__GH_READY" != true ]]; then
        _release_steps__step "would list the pull requests open into develop of $repository"
        return 0
    fi
    _release_steps__step "listing the pull requests open into develop of $repository"
    _release_steps__read_github "the open pull requests" "$repository" "" true \
        gh pr list --repo "$repository" --base develop --state open --json number,title \
        --jq '.[] | "#\(.number) \(.title)"' || status=$?
    [[ "$status" -eq 0 ]] || _release_steps__die_after_retries "the open pull requests" "$repository"
    if [[ -z "$_RELEASE_STEPS__ANSWER" ]]; then
        _release_steps__ok "no pull request is open into develop"
    else
        printf 'release-steps: open: %s\n' "${_RELEASE_STEPS__ANSWER//$'\n'/$'\n'release-steps: open: }"
        _release_steps__ok "$note"
    fi
}

# _release_steps__open_worktree <directory>: check out the checkout's develop, detached, in a scratch worktree, in
# which the command writes and commits its change, so the operator's tree is not touched and no branch exists until
# the commit does.
_release_steps__open_worktree() {
    local directory="$1" answer
    _release_steps__create_temp_dir
    _release_steps__step "checking out develop of $directory in a scratch worktree"
    answer="$(git -C "$directory" worktree add --quiet --detach "$_RELEASE_STEPS__TEMP_DIR/tree" develop 2>&1)" \
        || _release_steps__die "\`git worktree add\` failed in $directory (${answer//$'\n'/ })"
    _RELEASE_STEPS__WORKTREE="$_RELEASE_STEPS__TEMP_DIR/tree"
    _RELEASE_STEPS__WORKTREE_REPOSITORY="$directory"
}

# _release_steps__require_worktree_change <file>=<added>/<removed>...: refuse unless `git diff --numstat` in the
# scratch worktree lists these files alone, each with these line counts, where `+` stands for one line or more, so
# a rewrite that did more or less than the command asked is caught before it is committed. Prints the counts as
# the step's `ok:` line.
_release_steps__require_worktree_change() {
    local expected file counts added removed line line_added line_removed found summary=""
    local numstat="$_RELEASE_STEPS__TEMP_DIR/numstat"
    git -C "$_RELEASE_STEPS__WORKTREE" diff --numstat >"$numstat" || _release_steps__die "\`git diff\` failed in the scratch worktree"
    [[ "$(wc -l <"$numstat")" -eq "$#" ]] \
        || _release_steps__die "the scratch worktree changes $(wc -l <"$numstat") files, not the $# the command writes ($(_release_steps__format_one_line "$(<"$numstat")")); nothing was committed"
    for expected in "$@"; do
        file="${expected%%=*}" counts="${expected#*=}" added="${counts%/*}" removed="${counts#*/}" found=""
        while IFS=$'\t' read -r line_added line_removed line; do
            [[ "$line" == "$file" ]] || continue
            found=true
            [[ (("$added" == + && "$line_added" =~ ^[1-9][0-9]*$) || "$line_added" == "$added") \
                && "$line_removed" == "$removed" ]] \
                || _release_steps__die "the scratch worktree changes $file by +$line_added -$line_removed lines, not the +$added -$removed the command writes; nothing was committed"
            summary+="${summary:+, }$file +$line_added -$line_removed"
        done <"$numstat"
        [[ -n "$found" ]] || _release_steps__die "the scratch worktree does not change $file; nothing was committed"
    done
    _release_steps__ok "$summary in the scratch worktree"
}

# _release_steps__format_wrapped <width> <first-prefix> <prefix> <text>: print <text> filled to <width>
# columns, the first line opening with <first-prefix> and each later one with <prefix>. A `code span` is not broken.
_release_steps__format_wrapped() {
    local width="$1" prefix="$2" rest="$3" text="$4" protected="" inside=false character index line="" word words
    for ((index = 0; index < ${#text}; index++)); do
        character="${text:index:1}"
        if [[ "$character" == '`' ]]; then
            [[ "$inside" == true ]] && inside=false || inside=true
        elif [[ "$character" == " " && "$inside" == true ]]; then
            character=$'\x1f'
        fi
        protected+="$character"
    done
    read -ra words <<<"$protected"
    for word in "${words[@]}"; do
        if [[ -z "$line" ]]; then
            line="$prefix$word"
        elif [[ $((${#line} + 1 + ${#word})) -le "$width" ]]; then
            line+=" $word"
        else
            printf '%s\n' "${line//$'\x1f'/ }"
            prefix="$rest"
            line="$prefix$word"
        fi
    done
    [[ -z "$line" ]] || printf '%s\n' "${line//$'\x1f'/ }"
}

# _release_steps__format_list <item>...: print the items as prose: `a`, `a and b`, `a, b and c`.
_release_steps__format_list() {
    local text=""
    while [[ "$#" -gt 2 ]]; do
        text+="$1, "
        shift
    done
    [[ "$#" -eq 2 ]] && text+="$1 and $2" || text+="$1"
    printf '%s' "$text"
}

# _release_steps__format_joined <separator> <item>...: print the items joined by <separator>.
_release_steps__format_joined() {
    local separator="$1" text=""
    shift
    while [[ "$#" -gt 0 ]]; do
        text+="${text:+$separator}$1"
        shift
    done
    printf '%s' "$text"
}

# _release_steps__replace_line <file> <line-number> <text>: replace one line of <file> in place, which keeps the
# file's mode, once the rewritten text is checked line by line against the file: <text> at that line and every other
# line unchanged, so a failed or partial awk run leaves the file as it was. The text reaches awk through the
# environment, which does not interpret a backslash.
_release_steps__replace_line() {
    local file="$1" number="$2" text="$3" rewritten index last before=() after=()
    [[ "$number" =~ ^[1-9][0-9]*$ ]] || _release_steps__die "$file holds no single line to rewrite (\`$number\`); the file is unchanged"
    _release_steps__create_temp_dir
    rewritten="$_RELEASE_STEPS__TEMP_DIR/rewritten"
    _RELEASE_STEPS__LINE="$text" awk -v number="$number" \
        'NR == number { print ENVIRON["_RELEASE_STEPS__LINE"]; next } { print }' "$file" >"$rewritten" \
        || _release_steps__die "awk did not rewrite $file:$number; the file is unchanged"
    # awk ends every line with a newline; a file whose last line has none keeps that, so the diff holds one line.
    _release_steps__read_output last tail -c 1 "$file" \
        || _release_steps__die "the last byte of $file could not be read ($_RELEASE_STEPS__ANSWER); the file is unchanged"
    if [[ -n "$last" ]]; then
        truncate -s -1 "$rewritten" || _release_steps__die "the rewrite of $file could not be trimmed; the file is unchanged"
    fi
    mapfile -t before <"$file"
    mapfile -t after <"$rewritten"
    [[ "${#before[@]}" -eq "${#after[@]}" && "$number" -le "${#before[@]}" ]] \
        || _release_steps__die "the rewrite of $file:$number holds ${#after[@]} lines, not ${#before[@]}; the file is unchanged"
    for index in "${!before[@]}"; do
        if [[ "$index" -eq $((number - 1)) ]]; then
            [[ "${after[index]}" == "$text" ]]
        else
            [[ "${after[index]}" == "${before[index]}" ]]
        fi || _release_steps__die "the rewrite of $file:$number changed line $((index + 1)); the file is unchanged"
    done
    cat "$rewritten" >"$file" || _release_steps__die "$file could not be written"
}

# _release_steps__read_branch_date <directory> <branch> <variable>: set _RELEASE_STEPS__BRANCH_DATE to the
# `# resolved <date>` of the <variable> pin line at the tip of a local <branch>, so a re-run regenerates the change
# the branch committed on an earlier day; returns 1 where the branch or the comment is absent, and refuses where
# the branch exists and its script cannot be read, since today's date would then regenerate another change.
_release_steps__read_branch_date() {
    local script line
    _RELEASE_STEPS__BRANCH_DATE=""
    git -C "$1" rev-parse --verify --quiet "refs/heads/$2" >/dev/null || return 1
    _release_steps__read_output script git -C "$1" show "refs/heads/$2:packaging/release-steps.sh" \
        || _release_steps__die "branch $2 exists and its packaging/release-steps.sh could not be read in $1 ($_RELEASE_STEPS__ANSWER); nothing was written: run the same command again"
    line="$(grep -E "^$3=" <<<"$script")" || return 1
    [[ "$line" =~ \#\ resolved\ ([0-9]{4}-[0-9]{2}-[0-9]{2})$ ]] || return 1
    _RELEASE_STEPS__BRANCH_DATE="${BASH_REMATCH[1]}"
}

# _release_steps__read_newest_version <changelog-text>: print the version of the newest `## [X.Y.Z]` heading.
_release_steps__read_newest_version() {
    local version
    version="$(grep -m1 -oE '^## \[[0-9]+\.[0-9]+\.[0-9]+[^]]*\]' <<<"$1")" \
        || _release_steps__die "\`CHANGELOG.md\` has no \`## [X.Y.Z]\` heading; the entry needs the previous version"
    version="${version#'## ['}"
    printf '%s' "${version%]}"
}

# _release_steps__insert_changelog_entry <changelog-file> <entry-lines>: add the entry under
# `## [Unreleased]`, at the end of its `### Fixed` list, which is created ahead of a `### Security` heading or at
# the section's end when the section has none.
_release_steps__insert_changelog_entry() {
    local file="$1" entry="$2" lines=() index unreleased="" end fixed="" target insert block=()
    mapfile -t lines <"$file"
    for index in "${!lines[@]}"; do
        if [[ -z "$unreleased" && "${lines[index]}" == "## [Unreleased]" ]]; then
            unreleased="$index"
        fi
    done
    [[ -n "$unreleased" ]] \
        || _release_steps__die "\`CHANGELOG.md\` has no \`## [Unreleased]\` section; add it above the newest release heading, then run again"
    end="${#lines[@]}"
    for ((index = unreleased + 1; index < ${#lines[@]}; index++)); do
        [[ "${lines[index]}" == "## ["* ]] && { end="$index"; break; }
    done
    target="$end"
    for ((index = unreleased + 1; index < end; index++)); do
        [[ "${lines[index]}" == "### Fixed" ]] && fixed="$index"
        [[ -z "$fixed" && "${lines[index]}" == "### Security" ]] && target="$index"
    done
    mapfile -t block <<<"$entry"
    if [[ -n "$fixed" ]]; then
        target="$end"
        for ((index = fixed + 1; index < end; index++)); do
            [[ "${lines[index]}" == "### "* ]] && { target="$index"; break; }
        done
        insert="$target"
        while [[ "$insert" -gt $((fixed + 1)) && -z "${lines[insert - 1]}" ]]; do insert=$((insert - 1)); done
        [[ "$insert" -eq $((fixed + 1)) ]] && block=("" "${block[@]}")
    else
        insert="$target"
        while [[ "$insert" -gt $((unreleased + 1)) && -z "${lines[insert - 1]}" ]]; do insert=$((insert - 1)); done
        block=("" "### Fixed" "" "${block[@]}")
    fi
    [[ "$insert" -lt "${#lines[@]}" && -n "${lines[insert]}" ]] && block+=("")
    printf '%s\n' "${lines[@]:0:insert}" "${block[@]}" "${lines[@]:insert}" >"$file"
}

# _release_steps__deliver_change <directory> <branch> <title> <body>: turn the change in the worktree into
# a signed-off commit, the branch, a push and a pull request into develop, continuing from whichever of them already
# exists. The commit is made on the worktree's detached HEAD and the branch is then created at it, so a commit that
# fails does not leave a branch behind and a re-run starts over. A dry run runs the branch checks, then prints the
# diff, the title and the body instead.
_release_steps__deliver_change() {
    local directory="$1" branch="$2" title="$3" body="$4" worktree="$_RELEASE_STEPS__WORKTREE"
    local repository local_tip="" remote_tip answer develop message body_file attempt wait url status=0
    _release_steps__read_github_repository "$directory"
    repository="$_RELEASE_STEPS__GITHUB_REPOSITORY"
    _release_steps__read_output remote_tip git -C "$directory" ls-remote origin "refs/heads/$branch" \
        || _release_steps__die "the origin of $directory could not be read ($_RELEASE_STEPS__ANSWER); check the network, then run again"
    remote_tip="${remote_tip%%[[:space:]]*}"
    local_tip="$(git -C "$directory" rev-parse --verify --quiet "refs/heads/$branch")" || local_tip=""
    develop="$(git -C "$directory" rev-parse develop)" \
        || _release_steps__die "develop could not be read in $directory; no commit was made and nothing was pushed: run the same command again"
    if [[ -n "$local_tip" ]]; then
        if [[ "$(git -C "$directory" rev-parse --verify --quiet "$local_tip^")" != "$develop" ]] \
                || ! git -C "$worktree" diff --quiet "$local_tip" --; then
            _release_steps__die "branch $branch exists with a different change (${local_tip:0:7}); delete it or pick the next letter for \`--id\`"
        fi
        _release_steps__ok "branch $branch already carries this change; continuing from it"
    fi
    [[ -z "$remote_tip" || "$remote_tip" == "$local_tip" ]] \
        || _release_steps__die "branch $branch already exists on origin; pick the next letter for \`--id\`"
    if [[ "$_RELEASE_STEPS__DRY_RUN" == true ]]; then
        _release_steps__step "would commit on $branch:"
        git -C "$worktree" --no-pager diff
        printf '\n%s\n\n%s\n\n' "$title" "$body"
        _release_steps__step "would push $branch to origin and open the pull request \"$title\" into develop of $repository"
        return 0
    fi
    message="$_RELEASE_STEPS__TEMP_DIR/message"
    # The body file is written before the commit, so a failure to write it refuses before a commit or a branch exists.
    if ! body_file="$(git -C "$directory" rev-parse --path-format=absolute --git-common-dir)/release-steps/${branch//\//-}.md" \
            || ! mkdir -p "${body_file%/*}" || ! printf '%s\n\n%s\n' "$title" "$body" >"$message" \
            || ! printf '%s\n' "$body" >"$body_file"; then
        _release_steps__die "the commit message or the pull request body for $branch could not be written; no commit was made and nothing was pushed: run the same command again"
    fi
    if [[ -z "$local_tip" ]]; then
        _release_steps__step "committing on $branch"
        answer="$(git -C "$worktree" commit --quiet -s -a -F "$message" 2>&1)" \
            || _release_steps__die "the commit on $branch failed (${answer//$'\n'/ }); no branch was created and nothing was pushed"
        local_tip="$(git -C "$worktree" rev-parse HEAD)" \
            || _release_steps__die "the commit on $branch was made in the scratch worktree and could not be read back; no branch was created and nothing was pushed: run the same command again"
        answer="$(git -C "$directory" update-ref "refs/heads/$branch" "$local_tip" "" 2>&1)" \
            || _release_steps__die "branch $branch could not be created at ${local_tip:0:7} (${answer//$'\n'/ }); nothing was pushed: run the same command again"
        _release_steps__ok "commit ${local_tip:0:7} (signed off by $(git -C "$directory" config user.name))"
    fi
    if [[ "$remote_tip" == "$local_tip" ]]; then
        _release_steps__ok "origin/$branch = ${local_tip:0:7}, pushed before"
    else
        _release_steps__step "pushing $branch"
        answer="$(git -C "$directory" push --quiet origin "refs/heads/$branch:refs/heads/$branch" 2>&1)" \
            || _release_steps__die "the push of $branch was refused (${answer//$'\n'/ }); the branch is local and nothing was opened; fix the cause and run again"
        _release_steps__read_output remote_tip git -C "$directory" ls-remote origin "refs/heads/$branch" \
            || _release_steps__die "the push of $branch ran and origin could not be read after it ($_RELEASE_STEPS__ANSWER); the pull request is not open: run the same command again"
        [[ "${remote_tip%%[[:space:]]*}" == "$local_tip" ]] \
            || _release_steps__die "after the push, origin/$branch is ${remote_tip:-absent}, not ${local_tip:0:7}; run the same command again"
        _release_steps__ok "origin/$branch = ${local_tip:0:7}"
    fi
    _release_steps__read_github "the pull request lookup" "$branch" "" true \
        gh pr list --repo "$repository" --head "$branch" --base develop --state open --json url \
        --jq '.[0].url // empty' || status=$?
    [[ "$status" -eq 0 ]] || _release_steps__die_after_retries "the pull request lookup" "$branch"
    if [[ -n "$_RELEASE_STEPS__ANSWER" ]]; then
        _RELEASE_STEPS__PULL_REQUEST_URL="$_RELEASE_STEPS__ANSWER"
        _release_steps__ok "pull request $_RELEASE_STEPS__ANSWER is open for $branch; nothing to create"
        return 0
    fi
    _release_steps__step "opening the pull request"
    for ((attempt = 1; attempt <= _RELEASE_STEPS__RETRY_ATTEMPTS; attempt++)); do
        if answer="$(gh pr create --repo "$repository" --base develop --head "$branch" --title "$title" \
                --body-file "$body_file" 2>&1)"; then
            # The pull request exists once gh succeeds, so an answer without a URL ends in the `ok:` line, not a refusal.
            url="$(grep -oE 'https://[^[:space:]]+' <<<"$answer" | tail -n 1)" || url=""
            _RELEASE_STEPS__PULL_REQUEST_URL="$url"
            _release_steps__ok "${url:-the pull request is open}"
            return 0
        fi
        answer="${answer//$'\n'/ }"
        [[ "$answer" =~ $_RELEASE_STEPS__BRANCH_NOT_SERVED_PATTERN \
            && "$attempt" -lt "$_RELEASE_STEPS__RETRY_ATTEMPTS" ]] || break
        wait=$((attempt * _RELEASE_STEPS__RETRY_SECONDS))
        echo "release-steps: retry $attempt/$_RELEASE_STEPS__RETRY_ATTEMPTS: the pull request for $branch ($answer); waiting $wait s"
        sleep "$wait"
    done
    _release_steps__die "branch $branch is pushed; the pull request was not created ($answer); retry with: gh pr create --repo $repository --base develop --head $branch --title \"$title\" --body-file \"$body_file\""
}

# _release_steps__require_caller_ready <caller>: repin-tools' preconditions in a caller's checkout, run by
# refresh-images before it writes, so a caller that is not ready refuses the refresh while the tools are unchanged.
_release_steps__require_caller_ready() {
    local caller="$1" branches name
    _RELEASE_STEPS__DIE_PREFIX="caller $caller: "
    _RELEASE_STEPS__DIE_SUFFIX="; fix it before the refresh, nothing in ai-tools-assets-tools was changed"
    _release_steps__require_caller_checkout "$caller"
    _release_steps__require_synced_develop "$caller"
    _release_steps__read_caller_pins "$caller"
    [[ "${#_RELEASE_STEPS__CALLER_REFS[@]}" -gt 0 ]] \
        || _release_steps__die "no \`uses: $_RELEASE_STEPS__TOOLS_REPOSITORY/.github/workflows/…\` line under .github/workflows/; nothing to re-pin"
    _release_steps__require_equal_caller_pins
    _release_steps__read_output branches git -C "$caller" ls-remote origin "refs/heads/ci/*" \
        || _release_steps__die "the origin of $caller could not be read ($_RELEASE_STEPS__ANSWER)"
    while IFS=$'\t' read -r _ name; do
        if [[ "$name" == "refs/heads/ci/$_RELEASE_STEPS__ID-tools-"* ]]; then
            _release_steps__die "branch ${name#refs/heads/} already exists on origin; pick the next letter for \`--id\`"
        fi
    done <<<"$branches"
    _RELEASE_STEPS__DIE_PREFIX=""
    _RELEASE_STEPS__DIE_SUFFIX=""
    _release_steps__ok "caller $caller is ready: its \`uses:\` lines pin @${_RELEASE_STEPS__CALLER_REFS[0]}"
}

# refresh-images: half one of a digest refresh, run in a checkout of the tools.
_release_steps__refresh_images() {
    local caller dist dists=() names=() registries=() references=() quoted=() old=() new=() pairs=() resolving=()
    local today previous branch title noun images serves entry body file count variable reference tools_path
    _release_steps__parse_change_arguments refresh-images "$@"
    _release_steps__require_tools_checkout
    _release_steps__require_gh
    _release_steps__require_synced_develop "$PWD"
    for caller in "${_RELEASE_STEPS__CALLERS[@]}"; do
        _release_steps__require_caller_ready "$caller"
    done

    _release_steps__parse_image_pins "$(<"$PWD/packaging/release-steps.sh")"
    _release_steps__step "checking ${#_RELEASE_STEPS__PIN_DISTS[@]} images"
    _release_steps__check_pins
    [[ "$_RELEASE_STEPS__FAILED_COUNT" -eq "${#_RELEASE_STEPS__EXPIRED_DISTS[@]}" ]] \
        || _release_steps__die "check-images could not read every image (the lines above), so an expiry cannot be told from an outage: retry later, nothing was changed"
    if [[ "${#_RELEASE_STEPS__EXPIRED_DISTS[@]}" -eq 0 ]]; then
        _release_steps__ok "every image resolves; nothing to refresh"
        return 0
    fi
    for dist in "${_RELEASE_STEPS__PIN_DISTS[@]}"; do
        if [[ " ${_RELEASE_STEPS__EXPIRED_DISTS[*]} " != *" $dist "* ]]; then
            resolving+=("$dist")
            continue
        fi
        [[ -n "${_RELEASE_STEPS__DIST_NAMES[$dist]:-}" ]] \
            || _release_steps__die "the dist tag $dist has no name in _RELEASE_STEPS__DIST_NAMES; add it beside DIST_IMAGES"
        reference="${_RELEASE_STEPS__PIN_REFERENCES[$dist]}"
        dists+=("$dist")
        names+=("${_RELEASE_STEPS__DIST_NAMES[$dist]}")
        references+=("${reference%@*}")
        quoted+=("\`${reference%@*}\`")
        [[ " ${registries[*]} " == *" ${reference%%/*} "* ]] || registries+=("${reference%%/*}")
        old+=("${reference##*@sha256:}")
        new+=("${_RELEASE_STEPS__CURRENT_DIGESTS[$dist]#sha256:}")
        pairs+=("${old[-1]:0:12} -> ${new[-1]:0:12}")
    done
    _release_steps__ok "$(_release_steps__format_joined ", " "${dists[@]}") expired ($(_release_steps__format_joined ", " "${pairs[@]}"))${resolving[*]:+; $(_release_steps__format_joined ", " "${resolving[@]}") resolve}"

    noun=image images="$(_release_steps__format_list "${names[@]}")"
    [[ "${#dists[@]}" -eq 1 ]] || noun=images
    serves="$(_release_steps__format_list "${registries[@]}")"
    branch="build/$_RELEASE_STEPS__ID-$(_release_steps__format_joined - "${dists[@]}")-digest"
    title="build(packaging): pin the $images $noun $serves serves"
    # A re-run keeps the date the branch committed, so the regenerated change equals the branch's on a later day.
    if _release_steps__read_branch_date "$PWD" "$branch" "${dists[0]^^}_IMAGE"; then
        today="$_RELEASE_STEPS__BRANCH_DATE"
    else
        today="$(date -u +%Y-%m-%d)"
    fi
    _release_steps__open_worktree "$PWD"
    _release_steps__step "writing packaging/release-steps.sh, CHANGELOG.md in the scratch worktree"
    file="$_RELEASE_STEPS__WORKTREE/packaging/release-steps.sh"
    for count in "${!dists[@]}"; do
        variable="${dists[count]^^}_IMAGE"
        _release_steps__replace_line "$file" "$(grep -n "^$variable=" "$file" | cut -d: -f1)" \
            "$variable=${references[count]}@sha256:${new[count]} # resolved $today"
    done
    previous="$(_release_steps__read_newest_version "$(<"$_RELEASE_STEPS__WORKTREE/CHANGELOG.md")")"
    if [[ "${#dists[@]}" -eq 1 ]]; then
        entry="**$images image.** A release verifies its $images RPM in the image $serves serves for ${quoted[0]} on $today. $serves retired the digest $previous pinned, so \`check-images\` reported \`manifest unknown\` on every pull request."
    else
        entry="**$images images.** A release verifies its $images RPMs in the images $serves serves for $(_release_steps__format_list "${quoted[@]}") on $today. $serves retired the digests $previous pinned, so \`check-images\` reported \`manifest unknown\` on every pull request."
    fi
    _release_steps__insert_changelog_entry "$_RELEASE_STEPS__WORKTREE/CHANGELOG.md" \
        "$(_release_steps__format_wrapped 79 "- " "  " "$entry")"
    _release_steps__require_worktree_change "packaging/release-steps.sh=${#dists[@]}/${#dists[@]}" "CHANGELOG.md=+/0"

    body="$(_release_steps__format_refresh_body "$images" "$today" "$serves" "${references[*]}" "${old[*]}" \
        "${new[*]}")"
    _release_steps__list_open_pull_requests "$_RELEASE_STEPS__TOOLS_REPOSITORY" \
        "each of them passes on a re-run once the refresh is on develop"
    _release_steps__deliver_change "$PWD" "$branch" "$title" "$body"

    if [[ "$_RELEASE_STEPS__DRY_RUN" == true ]]; then
        _release_steps__next "to make the change: bash packaging/release-steps.sh refresh-images --id $_RELEASE_STEPS__ID${_RELEASE_STEPS__CALLERS[*]/#/ --caller }"
        return 0
    fi
    _release_steps__next "after the merge: git ls-remote $_RELEASE_STEPS__TOOLS_URL refs/heads/develop"
    if [[ "${#_RELEASE_STEPS__CALLERS[@]}" -eq 0 ]]; then
        _release_steps__next "in each caller: bash $PWD/packaging/release-steps.sh repin-tools --id $_RELEASE_STEPS__ID <sha>"
    fi
    for caller in "${_RELEASE_STEPS__CALLERS[@]}"; do
        tools_path="$(realpath --relative-to="$caller" "$PWD")"
        _release_steps__next "in $caller: bash $tools_path/packaging/release-steps.sh repin-tools --id $_RELEASE_STEPS__ID <sha>"
    done
}

# _release_steps__format_refresh_body <distros> <date> <registries> <references> <old> <new>: print the commit
# and pull-request body of a digest refresh in the tools; <references>, <old> and <new> are space-separated, one
# element per refreshed image.
_release_steps__format_refresh_body() {
    local images="$1" today="$2" serves="$3" references old new index olds=() news=() refs=()
    read -ra references <<<"$4"
    read -ra old <<<"$5"
    read -ra new <<<"$6"
    for index in "${!references[@]}"; do
        refs+=("\`${references[index]}\`")
        olds+=("\`${old[index]:0:12}\`")
        news+=("\`${new[index]:0:12}\`")
    done
    if [[ "${#references[@]}" -eq 1 ]]; then
        _release_steps__format_wrapped 72 "" "" "$serves retired the $images digest the release steps pinned (${olds[0]}), so \`check-images\` fails every pull request and push here and in each caller with \`manifest unknown\`. The steps pin the digest ${refs[0]} resolves to on $today (${news[0]})."
    else
        _release_steps__format_wrapped 72 "" "" "$serves retired the $images digests the release steps pinned ($(_release_steps__format_joined ", " "${olds[@]}")), so \`check-images\` fails every pull request and push here and in each caller with \`manifest unknown\`. The steps pin the digests $(_release_steps__format_list "${refs[@]}") resolve to on $today ($(_release_steps__format_joined ", " "${news[@]}"))."
    fi
    echo
    _release_steps__format_wrapped 72 "" "" "A caller re-pins its \`uses:\` lines to this commit once it is on \`develop\`: \`release-steps.sh repin-tools --id $_RELEASE_STEPS__ID <sha>\`. No release is cut for a refresh; the next release carries it."
}

# _release_steps__read_tag_signers <tools-dir>: set _RELEASE_STEPS__TAG_SIGNERS and _RELEASE_STEPS__TAG_SIGNER_KEY_URL
# to the primaries and the key URL the tools' release.yml pins for verify-tag, so repin-tools accepts the tags
# a release of the tools accepts.
_release_steps__read_tag_signers() {
    local file="$1/.github/workflows/release.yml" line
    _RELEASE_STEPS__TAG_SIGNERS=""
    _RELEASE_STEPS__TAG_SIGNER_KEY_URL=""
    [[ -f "$file" ]] || _release_steps__die "$file is absent; repin-tools reads the tag signers verify-tag pins from it"
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]+TAG_SIGNER_PRIMARY_FINGERPRINTS:[[:space:]]+([0-9A-F][0-9A-F ]*[0-9A-F])[[:space:]]*$ ]]; then
            _RELEASE_STEPS__TAG_SIGNERS="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^[[:space:]]+TAG_SIGNER_KEY_URL:[[:space:]]+(https://[^[:space:]]+)[[:space:]]*$ ]]; then
            _RELEASE_STEPS__TAG_SIGNER_KEY_URL="${BASH_REMATCH[1]}"
        fi
    done <"$file"
    [[ -n "$_RELEASE_STEPS__TAG_SIGNERS" && -n "$_RELEASE_STEPS__TAG_SIGNER_KEY_URL" ]] \
        || _release_steps__die "$file does not set TAG_SIGNER_PRIMARY_FINGERPRINTS and an https TAG_SIGNER_KEY_URL"
}

# _release_steps__list_release_headlines <changelog-text> <version>: print one headline per entry of the
# `## [<version>]` section: its bold label, or else its first sentence, without the closing period.
_release_steps__list_release_headlines() {
    local version="$2" line entry="" next inside=false
    while IFS= read -r line; do
        if [[ "$line" == "## ["* ]]; then
            [[ "$inside" == true ]] && break
            [[ "$line" == "## [$version]"* ]] && inside=true
            continue
        fi
        [[ "$inside" == true ]] || continue
        if [[ "$line" =~ ^-[[:space:]]+(.*)$ ]]; then
            # The capture is saved first: format_headline runs a regex of its own, which replaces BASH_REMATCH.
            next="${BASH_REMATCH[1]}"
            _release_steps__format_headline "$entry"
            entry="$next"
        elif [[ -n "$entry" && "$line" =~ ^[[:space:]]+(.*)$ ]]; then
            entry+=" ${BASH_REMATCH[1]}"
        else
            _release_steps__format_headline "$entry"
            entry=""
        fi
    done <<<"$1"
    _release_steps__format_headline "$entry"
}

# _release_steps__format_headline <entry>: print the headline of one changelog entry, and no line for an empty entry.
_release_steps__format_headline() {
    local entry="$1" headline
    [[ -n "$entry" ]] || return 0
    if [[ "$entry" =~ ^\*\*([^*]+)\*\* ]]; then
        headline="${BASH_REMATCH[1]}"
    else
        headline="${entry%%. *}"
    fi
    printf '%s\n' "${headline%.}"
}

# _release_steps__resolve_caller_pin <tools-dir> <ref>: set _RELEASE_STEPS__CALLER_PIN_COMMIT to the commit the
# caller's `uses:` lines name on the tools' origin. A full SHA names one commit wherever it is held, so it is read from
# the checkout and fetched only where the checkout lacks it; a tag or a branch name moves, so it is read from origin now
# and fetched into FETCH_HEAD alone, which leaves the checkout's own tags as they are.
_release_steps__resolve_caller_pin() {
    local tools="$1" ref="$2" commit="" remote object name names=()
    if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
        commit="$(git -C "$tools" rev-parse --verify --quiet "$ref^{commit}")" || commit=""
        if [[ -z "$commit" ]] && git -C "$tools" fetch --quiet --no-tags origin "$ref" 2>/dev/null; then
            commit="$(git -C "$tools" rev-parse --verify --quiet "FETCH_HEAD^{commit}")" || commit=""
        fi
    else
        _release_steps__read_output remote git -C "$tools" ls-remote origin "refs/tags/$ref" "refs/heads/$ref" \
            || _release_steps__die "the origin of ai-tools-assets-tools could not be read ($_RELEASE_STEPS__ANSWER); check the network, then run again"
        while IFS=$'\t' read -r object name; do
            if [[ -n "$object" && ("$name" == "refs/tags/$ref" || "$name" == "refs/heads/$ref") ]]; then
                names+=("$name")
            fi
        done <<<"$remote"
        # A name both a tag and a branch carry is ambiguous (GitHub picks one), so it is refused rather than resolved.
        [[ "${#names[@]}" -le 1 ]] \
            || _release_steps__die "the \`uses:\` lines pin @$ref, which names both a tag and a branch on ai-tools-assets-tools origin; pin a tag or a full commit SHA, then run again"
        remote="${names[0]:-}"
        if [[ -n "$remote" ]] && git -C "$tools" fetch --quiet --no-tags origin "$remote" 2>/dev/null; then
            commit="$(git -C "$tools" rev-parse --verify --quiet "FETCH_HEAD^{commit}")" || commit=""
        fi
    fi
    [[ -n "$commit" ]] \
        || _release_steps__die "the \`uses:\` lines pin @$ref, which is not a commit, a tag or a branch on ai-tools-assets-tools origin; fix the pins, then run again"
    _RELEASE_STEPS__CALLER_PIN_COMMIT="$commit"
}

# _release_steps__require_pin_change <tools-dir> <base> <sha> <framing> <rule>: refuse unless the tree of <sha>
# differs from that of <base> in CHANGELOG.md and <DIST>_IMAGE pin lines alone, at least one of them. A refusal opens
# with <framing> (`<sha> changes`, or `from the pinned <commit>, <sha> changes`), names the file and the line, and ends
# with <rule>. Sets _RELEASE_STEPS__REFRESH_DISTS to the dist tags whose pin differs, in DIST_IMAGES order, and
# _RELEASE_STEPS__REFRESH_DATES to the `# resolved` date each one's line carries at <sha>, which refresh-images writes
# and the caller's comment and body name, so a pin line without the date is refused.
_release_steps__require_pin_change() {
    local tools="$1" base="$2" sha="$3" framing="$4" rule="$5" file line old_line new_line variable dist changed=()
    local names script hunks unread="nothing was written: run the same command again"
    _release_steps__read_output names git -C "$tools" diff --name-only "$base" "$sha" \
        || _release_steps__die "the files ${sha:0:7} changes against $base could not be listed in $tools (\`git diff\`: $_RELEASE_STEPS__ANSWER); $unread"
    while IFS= read -r file; do
        [[ -n "$file" ]] || continue
        [[ "$file" == packaging/release-steps.sh || "$file" == CHANGELOG.md ]] \
            || _release_steps__die "$framing $file, which is not an image pin or \`CHANGELOG.md\`; $rule"
    done <<<"$names"
    _release_steps__read_output script git -C "$tools" show "$sha:packaging/release-steps.sh" \
        || _release_steps__die "packaging/release-steps.sh at ${sha:0:7} could not be read in $tools (\`git show\`: $_RELEASE_STEPS__ANSWER); $unread"
    _release_steps__parse_image_pins "$script"
    _release_steps__read_output hunks git -C "$tools" diff --unified=0 --no-color "$base" "$sha" -- packaging/release-steps.sh \
        || _release_steps__die "the lines ${sha:0:7} changes in packaging/release-steps.sh against $base could not be read in $tools (\`git diff\`: $_RELEASE_STEPS__ANSWER); $unread"
    _RELEASE_STEPS__REFRESH_DATES=()
    while IFS= read -r line; do
        if [[ "$line" =~ ^@@\ -([0-9]+)(,[0-9]+)?\ \+([0-9]+) ]]; then
            old_line="${BASH_REMATCH[1]}" new_line="${BASH_REMATCH[3]}"
            continue
        fi
        [[ "$line" == [-+]* && "$line" != "---"* && "$line" != "+++"* ]] || continue
        variable=""
        [[ "$line" =~ ^[-+]([A-Z][A-Z0-9]*_IMAGE)= ]] && variable="${BASH_REMATCH[1]}"
        dist="${variable%_IMAGE}"
        dist="${dist,,}"
        if [[ -z "$variable" || " ${_RELEASE_STEPS__PIN_DISTS[*]} " != *" $dist "* ]]; then
            [[ "$line" == -* ]] && new_line="$old_line"
            _release_steps__die "$framing packaging/release-steps.sh:$new_line, which is not an image pin or \`CHANGELOG.md\`; $rule"
        fi
        if [[ "$line" == +* ]]; then
            [[ " ${changed[*]} " == *" $dist "* ]] || changed+=("$dist")
            [[ "$line" =~ \#\ resolved\ ([0-9]{4}-[0-9]{2}-[0-9]{2})$ ]] \
                || _release_steps__die "the $variable line at ${sha:0:7} carries no \`# resolved <date>\` comment, which refresh-images writes and the caller's pin comment names; refresh the pin with refresh-images"
            _RELEASE_STEPS__REFRESH_DATES[$dist]="${BASH_REMATCH[1]}"
            new_line=$((new_line + 1))
        else
            old_line=$((old_line + 1))
        fi
    done <<<"$hunks"
    [[ "${#changed[@]}" -gt 0 ]] || _release_steps__die "$framing no image pin; $rule"
    _RELEASE_STEPS__REFRESH_DISTS=()
    for dist in "${_RELEASE_STEPS__PIN_DISTS[@]}"; do
        # An `if`, not `[[ ]] && …`: a false test as the function's last command would be its status under `set -e`.
        if [[ " ${changed[*]} " == *" $dist "* ]]; then
            _RELEASE_STEPS__REFRESH_DISTS+=("$dist")
        fi
    done
}

# _release_steps__verify_release <tools-dir> <tag>: refuse unless <tag> is a final vX.Y.Z on the tools' origin,
# an annotated tag signed by a key the tools' release.yml pins, whose CHANGELOG.md opens with that version and lists
# its entries, and which has a GitHub release, not a prerelease or a draft, whose release run passed. Sets
# _RELEASE_STEPS__RELEASE_COMMIT, _RELEASE_STEPS__RELEASE_VERSION and _RELEASE_STEPS__RELEASE_HEADLINES.
_release_steps__verify_release() {
    local tools="$1" tag="$2" answer changelog newest status=0 sha jq_filter
    [[ "$tag" =~ ^v((0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))(-.*)?$ ]] \
        || _release_steps__die "give a commit SHA of the tools (a digest refresh) or a release tag \`vX.Y.Z\`"
    [[ -z "${BASH_REMATCH[5]}" ]] || _release_steps__die "$tag is a prerelease; a caller pins a final release"
    _RELEASE_STEPS__RELEASE_VERSION="${BASH_REMATCH[1]}"
    _release_steps__step "verifying tag $tag"
    # The tag is read from origin into FETCH_HEAD alone, so the checkout's own tag of that name is left as it is.
    _release_steps__read_output answer git -C "$tools" ls-remote origin "refs/tags/$tag" \
        || _release_steps__die "the origin of ai-tools-assets-tools could not be read ($_RELEASE_STEPS__ANSWER); check the network, then run again"
    [[ -n "$answer" ]] || _release_steps__die "tag $tag is not on ai-tools-assets-tools origin; push the tag, or check the spelling"
    answer="$(git -C "$tools" fetch --quiet --no-tags origin "refs/tags/$tag" 2>&1)" \
        || _release_steps__die "tag $tag did not fetch from ai-tools-assets-tools origin (${answer//$'\n'/ }); check the network, then run again"
    _release_steps__resolve_tag_object "$tools" "$tag" FETCH_HEAD
    _release_steps__read_tag_signers "$tools"
    _release_steps__verify_tag_signature "$tools" "$tag" "$_RELEASE_STEPS__TAG_OBJECT" "$_RELEASE_STEPS__TAG_SIGNERS" \
        "$_RELEASE_STEPS__TAG_SIGNER_KEY_URL"
    sha="$(git -C "$tools" rev-parse "$_RELEASE_STEPS__TAG_OBJECT^{commit}")" \
        || _release_steps__die "the commit of $tag could not be read in $tools; nothing was written: run the same command again"
    _RELEASE_STEPS__RELEASE_COMMIT="$sha"
    changelog="$(git -C "$tools" show "$sha:CHANGELOG.md")" \
        || _release_steps__die "\`CHANGELOG.md\` at $tag could not be read in $tools; nothing was written: run the same command again"
    newest="$(_release_steps__read_newest_version "$changelog")"
    [[ "$newest" == "$_RELEASE_STEPS__RELEASE_VERSION" ]] \
        || _release_steps__die "the tools' \`CHANGELOG.md\` at $tag opens its newest release with \`## [$newest]\`, not \`## [$_RELEASE_STEPS__RELEASE_VERSION]\`; the tag and the changelog disagree"
    _RELEASE_STEPS__RELEASE_HEADLINES="$(_release_steps__list_release_headlines "$changelog" "$newest")"
    [[ -n "$_RELEASE_STEPS__RELEASE_HEADLINES" ]] \
        || _release_steps__die "the \`## [$newest]\` section of \`CHANGELOG.md\` has no entries; the body needs at least one"
    _release_steps__ok "$tag is tag object ${_RELEASE_STEPS__TAG_OBJECT:0:7}, commit ${sha:0:7}, signed by $_RELEASE_STEPS__TAG_PRIMARY"
    if [[ "$_RELEASE_STEPS__GH_READY" != true ]]; then
        _release_steps__step "would check that $tag has a GitHub release whose release run passed"
        return 0
    fi
    _release_steps__read_github "the GitHub release" "$tag" "release not found|Not Found" false \
        gh release view "$tag" --repo "$_RELEASE_STEPS__TOOLS_REPOSITORY" --json isDraft,isPrerelease \
        --jq '"\(.isDraft) \(.isPrerelease)"' || status=$?
    [[ "$status" -ne 1 ]] || _release_steps__die "$tag has no GitHub release; a caller pins a final release"
    [[ "$status" -eq 0 ]] || _release_steps__die_after_retries "the GitHub release" "$tag"
    [[ "$_RELEASE_STEPS__ANSWER" == "false false" ]] \
        || _release_steps__die "$tag is a prerelease or a draft on GitHub; a caller pins a final release"
    jq_filter="[.workflow_runs[] | select(.path == \".github/workflows/release.yml\" and .head_sha == \"$sha\")][0]"
    jq_filter+=' | "\(.conclusion // "absent") \(.html_url // "")"'
    _release_steps__read_github "the release run" "$tag" "" false \
        gh api "repos/$_RELEASE_STEPS__TOOLS_REPOSITORY/actions/runs?event=push&branch=$tag&per_page=100" \
        --jq "$jq_filter" || status=$?
    [[ "$status" -eq 0 ]] || _release_steps__die_after_retries "the release run" "$tag"
    [[ "${_RELEASE_STEPS__ANSWER%% *}" == success ]] \
        || _release_steps__die "the release run for $tag concluded ${_RELEASE_STEPS__ANSWER%% *} (${_RELEASE_STEPS__ANSWER#* }); fix or re-run it before re-pinning"
    _release_steps__ok "$tag has a GitHub release whose release run passed (${_RELEASE_STEPS__ANSWER#* })"
}

# repin-tools: half two of a digest refresh, or a re-pin to a release of the tools, run in a caller's checkout from
# the tools' tree.
_release_steps__repin_tools() {
    local tools caller="$PWD" target sha mode index dist names=() quoted=() noun images serves registries=()
    local reference comment previous workflows=() workflow_list verb branch title body file files=() count script
    local pull_request status=0 date answer rule pinned old_comment pairs=() unreadable="" resolves=() changes=()
    local -A seen=() dates=()
    local parents parent_list=()
    _release_steps__parse_change_arguments repin-tools "$@"
    target="$_RELEASE_STEPS__TARGET"
    _release_steps__require_caller_checkout "$caller"
    _release_steps__require_tools_tree
    tools="$(_release_steps__read_toplevel "$PACKAGING")"
    _release_steps__require_gh
    _release_steps__require_synced_develop "$caller"
    _release_steps__read_caller_pins "$caller"
    [[ "${#_RELEASE_STEPS__CALLER_REFS[@]}" -gt 0 ]] \
        || _release_steps__die "no \`uses: $_RELEASE_STEPS__TOOLS_REPOSITORY/.github/workflows/…\` line under .github/workflows/; nothing to re-pin"
    _release_steps__require_equal_caller_pins

    _release_steps__step "fetching ai-tools-assets-tools origin/develop in $tools"
    answer="$(git -C "$tools" fetch --quiet --no-tags origin +refs/heads/develop:refs/remotes/origin/develop 2>&1)" \
        || _release_steps__die "\`git fetch origin develop\` failed in $tools (${answer//$'\n'/ }); check the network and the remote, then run again"
    _release_steps__ok "ai-tools-assets-tools origin/develop = $(git -C "$tools" rev-parse --short=7 origin/develop)"
    if [[ "$target" =~ ^v[0-9] ]]; then
        mode=release
        _release_steps__verify_release "$tools" "$target"
        sha="$_RELEASE_STEPS__RELEASE_COMMIT"
        comment="$target"
        branch="ci/$_RELEASE_STEPS__ID-tools-$_RELEASE_STEPS__RELEASE_VERSION"
        title="ci: call the ai-tools-assets-tools $_RELEASE_STEPS__RELEASE_VERSION workflows"
    elif [[ "$target" =~ ^[0-9a-f]{7,40}$ ]]; then
        mode=refresh
        sha="$(git -C "$tools" rev-parse --verify --quiet "$target^{commit}")" || sha=""
        if [[ -z "$sha" ]] || ! git -C "$tools" merge-base --is-ancestor "$sha" origin/develop; then
            _release_steps__die "$target is not on ai-tools-assets-tools \`origin/develop\` (fetched now); a develop pin names a merged commit: merge the pull request, then run this command again"
        fi
    else
        _release_steps__die "give a commit SHA of the tools (a digest refresh) or a release tag \`vX.Y.Z\`"
    fi

    count=0
    for index in "${!_RELEASE_STEPS__CALLER_REFS[@]}"; do
        [[ "${_RELEASE_STEPS__CALLER_REFS[index]}" == "$sha" ]] && count=$((count + 1))
    done
    if [[ "$count" -eq "${#_RELEASE_STEPS__CALLER_REFS[@]}" ]]; then
        _release_steps__ok "the \`uses:\` lines already pin @$sha; nothing to do"
        return 0
    fi

    if [[ "$mode" == refresh ]]; then
        # The commit is a refresh against its parent, and the move from the caller's pin to it brings a refresh alone:
        # a release tag and a release run attest to every other change, and a develop pin carries neither.
        rule="a develop pin is for a digest refresh alone, and any other change reaches a caller through a release"
        _release_steps__read_output parents git -C "$tools" rev-list --parents -n 1 "$sha" \
            || _release_steps__die "the parents of ${sha:0:7} could not be read in $tools ($_RELEASE_STEPS__ANSWER); nothing was written: run the same command again"
        read -ra parent_list <<<"$parents"
        [[ "${#parent_list[@]}" -eq 2 ]] || _release_steps__die "${sha:0:7} is not a commit with one parent; $rule"
        _release_steps__require_pin_change "$tools" "$sha^" "$sha" "${sha:0:7} changes" "$rule"
        _release_steps__resolve_caller_pin "$tools" "${_RELEASE_STEPS__CALLER_REFS[0]}"
        pinned="$_RELEASE_STEPS__CALLER_PIN_COMMIT"
        _release_steps__require_pin_change "$tools" "$pinned" "$sha" "from the pinned ${pinned:0:7}, ${sha:0:7} changes" \
            "a develop pin is for a digest refresh alone, so this caller re-pins through a release of the tools: cut the release, then run this command with its tag (\`vX.Y.Z\`)"
        if [[ -n "$_RELEASE_STEPS__PULL_REQUEST" ]]; then
            pull_request="$_RELEASE_STEPS__PULL_REQUEST"
        elif [[ "$_RELEASE_STEPS__GH_READY" == true ]]; then
            _release_steps__read_github "the pull request of commit" "${sha:0:7}" "" false \
                gh api "repos/$_RELEASE_STEPS__TOOLS_REPOSITORY/commits/$sha/pulls" --jq '.[0].number // empty' \
                || status=$?
            [[ "$status" -eq 0 ]] \
                || _release_steps__die "no pull request names commit ${sha:0:7} (\`gh api repos/$_RELEASE_STEPS__TOOLS_REPOSITORY/commits/${sha:0:7}/pulls\` answered: $_RELEASE_STEPS__ANSWER, $_RELEASE_STEPS__RETRY_ATTEMPTS times over $_RELEASE_STEPS__WAITED_SECONDS s); pass it with \`--pr <number>\`"
            pull_request="$_RELEASE_STEPS__ANSWER"
            [[ "$pull_request" =~ ^[1-9][0-9]*$ ]] \
                || _release_steps__die "GitHub names the pull request of commit ${sha:0:7} as \`$(_release_steps__format_one_line "$pull_request")\`, not a number; pass it with \`--pr <number>\`"
        else
            pull_request="<PR>"
        fi
        date=""
        for dist in "${_RELEASE_STEPS__REFRESH_DISTS[@]}"; do
            [[ -n "${_RELEASE_STEPS__DIST_NAMES[$dist]:-}" ]] \
                || _release_steps__die "the dist tag $dist has no name in _RELEASE_STEPS__DIST_NAMES; add it beside DIST_IMAGES"
            reference="${_RELEASE_STEPS__PIN_REFERENCES[$dist]}"
            names+=("${_RELEASE_STEPS__DIST_NAMES[$dist]}")
            quoted+=("\`${reference%@*}\`")
            resolves+=("the digest \`${reference%@*}\` resolves to on ${_RELEASE_STEPS__REFRESH_DATES[$dist]}")
            [[ " ${registries[*]} " == *" ${reference%%/*} "* ]] || registries+=("${reference%%/*}")
            # The comment names the newest resolution date; the dates are ISO, so the greatest string is the newest.
            [[ "${_RELEASE_STEPS__REFRESH_DATES[$dist]}" < "$date" ]] || date="${_RELEASE_STEPS__REFRESH_DATES[$dist]}"
            dates[${_RELEASE_STEPS__REFRESH_DATES[$dist]}]=1
        done
        noun=image images="$(_release_steps__format_list "${names[@]}")"
        [[ "${#names[@]}" -eq 1 ]] || noun=images
        serves="$(_release_steps__format_list "${registries[@]}")"
        comment="develop $date, $(_release_steps__format_joined - "${_RELEASE_STEPS__REFRESH_DISTS[@]}") digest, ai-tools-assets-tools#$pull_request"
        branch="ci/$_RELEASE_STEPS__ID-tools-$(_release_steps__format_joined - "${_RELEASE_STEPS__REFRESH_DISTS[@]}")-digest"
        title="ci: call the ai-tools-assets-tools commit that pins the $images $noun"
        _release_steps__ok "${sha:0:7} is on origin/develop and is ai-tools-assets-tools#$pull_request; from the pinned ${pinned:0:7} it changes image pins ($(_release_steps__format_joined ", " "${_RELEASE_STEPS__REFRESH_DISTS[@]}")) and CHANGELOG.md alone"
    fi

    _release_steps__read_output script git -C "$tools" show "$sha:packaging/release-steps.sh" \
        || _release_steps__die "packaging/release-steps.sh at ${sha:0:7} could not be read in $tools (\`git show\`: $_RELEASE_STEPS__ANSWER); nothing was written: run the same command again"
    _release_steps__parse_image_pins "$script"
    _release_steps__step "checking ${#_RELEASE_STEPS__PIN_DISTS[@]} images at ${sha:0:7}"
    _release_steps__check_pins
    [[ "$_RELEASE_STEPS__FAILED_COUNT" -eq 0 ]] \
        || _release_steps__die "the images ai-tools-assets-tools pins at ${sha:0:7} do not all resolve (the lines above); a caller pins a commit whose images resolve: run refresh-images in the tools first"
    _release_steps__ok "every image at ${sha:0:7} resolves"

    # The body names the previous pin from the old comments where every line carries one readable comment and they
    # are one value; readable comments that differ are sources that disagree, and a line without one leaves the ref
    # to stand for the pin.
    previous=""
    for index in "${!_RELEASE_STEPS__CALLER_COMMENTS[@]}"; do
        old_comment="${_RELEASE_STEPS__CALLER_COMMENTS[index]}"
        if [[ "$old_comment" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ || "$old_comment" =~ ^develop\  ]]; then
            seen[$old_comment]=1
            previous="$old_comment"
            pairs+=("${_RELEASE_STEPS__CALLER_FILES[index]}: \`# $old_comment\`")
        else
            unreadable="${unreadable:-${_RELEASE_STEPS__CALLER_FILES[index]}}"
        fi
    done
    [[ "${#seen[@]}" -le 1 ]] \
        || _release_steps__die "the \`uses:\` lines carry different pin comments ($(_release_steps__format_joined ", " "${pairs[@]}")); make them one, then run again"
    if [[ -n "$unreadable" || -z "$previous" ]]; then
        previous="${_RELEASE_STEPS__CALLER_REFS[0]}"
        [[ "$previous" =~ ^[0-9a-f]{40}$ ]] && previous="${previous:0:7}"
        _release_steps__warn "the comment after @${_RELEASE_STEPS__CALLER_REFS[0]} in $unreadable is not \`# vX.Y.Z\` or \`# develop …\`; the body names the previous pin as $previous"
    fi
    for file in "${_RELEASE_STEPS__CALLER_FILES[@]}"; do
        [[ " ${files[*]} " == *" $file "* ]] && continue
        files+=("$file")
        file="${file##*/}"
        workflows+=("${file%.y*ml}")
    done
    workflow_list="$(_release_steps__format_list "${workflows[@]}")"
    if [[ "${#workflows[@]}" -gt 1 ]]; then
        workflow_list+=" workflows" verb="They now call"
    else
        workflow_list+=" workflow" verb="It now calls"
    fi

    _release_steps__open_worktree "$caller"
    _release_steps__step "writing $(_release_steps__format_joined ", " "${files[@]}") in the scratch worktree"
    for index in "${!_RELEASE_STEPS__CALLER_FILES[@]}"; do
        _release_steps__replace_line "$_RELEASE_STEPS__WORKTREE/${_RELEASE_STEPS__CALLER_FILES[index]}" \
            "${_RELEASE_STEPS__CALLER_LINE_NUMBERS[index]}" \
            "${_RELEASE_STEPS__CALLER_PREFIXES[index]}$_RELEASE_STEPS__TOOLS_REPOSITORY/.github/workflows/${_RELEASE_STEPS__CALLER_WORKFLOWS[index]}@$sha # $comment"
    done
    for file in "${files[@]}"; do
        count=0
        for index in "${!_RELEASE_STEPS__CALLER_FILES[@]}"; do
            [[ "${_RELEASE_STEPS__CALLER_FILES[index]}" == "$file" ]] && count=$((count + 1))
        done
        changes+=("$file=$count/$count")
    done
    _release_steps__require_worktree_change "${changes[@]}"

    if [[ "$mode" == release ]]; then
        body="$(_release_steps__format_wrapped 72 "" "" "The $workflow_list previously called the $previous commit. ${verb} the $_RELEASE_STEPS__RELEASE_VERSION commit \`${sha:0:7}\` (release \`$target\`):")"
        body+=$'\n'
        while IFS= read -r reference; do
            body+=$'\n'"$(_release_steps__format_wrapped 72 "- " "  " "$reference")"
        done <<<"$_RELEASE_STEPS__RELEASE_HEADLINES"
    else
        # One date where every refreshed pin carries it; else each digest names the date its tag resolved to it.
        if [[ "$noun" == image ]]; then
            body="whose release steps pin a $images image digest $serves no longer serves"
            resolves=("which pins the digest ${quoted[0]} resolves to on $date")
        elif [[ "${#dates[@]}" -eq 1 ]]; then
            body="whose release steps pin $images image digests $serves no longer serves"
            resolves=("which pins the digests $(_release_steps__format_list "${quoted[@]}") resolve to on $date")
        else
            body="whose release steps pin $images image digests $serves no longer serves"
            resolves=("which pins $(_release_steps__format_list "${resolves[@]}")")
        fi
        body="$(_release_steps__format_wrapped 72 "" "" "The $workflow_list called the $previous commit, $body, so \`check-images\` failed every pull request here with \`manifest unknown\`. ${verb} the ai-tools-assets-tools \`develop\` commit \`${sha:0:7}\` ($_RELEASE_STEPS__TOOLS_REPOSITORY#$pull_request), ${resolves[0]}.")"
        body+=$'\n\n'"$(_release_steps__format_wrapped 72 "" "" "Not a release of the tools: the next tools release carries the digest, and the next re-pin here names that release.")"
    fi
    _release_steps__read_github_repository "$caller"
    _release_steps__list_open_pull_requests "$_RELEASE_STEPS__GITHUB_REPOSITORY" \
        "each of them passes on a re-run once this pin is on develop"
    _release_steps__deliver_change "$caller" "$branch" "$title" "$body"

    if [[ "$_RELEASE_STEPS__DRY_RUN" == true ]]; then
        _release_steps__next "to make the change: bash $0 repin-tools --id $_RELEASE_STEPS__ID $target${_RELEASE_STEPS__PULL_REQUEST:+ --pr $_RELEASE_STEPS__PULL_REQUEST}"
        return 0
    fi
    _release_steps__read_github_repository "$caller"
    _release_steps__next "after the merge: git ls-remote https://github.com/$_RELEASE_STEPS__GITHUB_REPOSITORY.git refs/heads/develop"
}

[[ "$#" -ge 1 ]] || _release_steps__die "usage: release-steps.sh <check-images|find-tests|test-floor|refresh-images|repin-tools|swap-off|verify-tag|preflight|sign-files|build-rpm|sign-rpm|create-release|dispatch> ..."
step="$1"
shift
case "$step" in
    check-images) _release_steps__check_images "$@" ;;
    find-tests) _release_steps__find_tests "$@" ;;
    test-floor) _release_steps__test_floor "$@" ;;
    refresh-images) _release_steps__refresh_images "$@" ;;
    repin-tools) _release_steps__repin_tools "$@" ;;
    swap-off) _release_steps__swap_off "$@" ;;
    verify-tag) _release_steps__verify_tag "$@" ;;
    preflight) _release_steps__run_preflight ;;
    sign-files) _release_steps__sign_files "$@" ;;
    build-rpm) _release_steps__build_rpm "$@" ;;
    sign-rpm) _release_steps__sign_rpm "$@" ;;
    create-release) _release_steps__create_release "$@" ;;
    dispatch) _release_steps__dispatch "$@" ;;
    *) _release_steps__die "unknown step $step" ;;
esac
