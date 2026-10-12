#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# The steps of a signed release, one subcommand per workflow step, shared by this repository's release.yml and the
# reusable set-release.yml a publisher calls, so the tag check, the key handling, the RPM signing and the dispatch exist
# in one copy. Each subcommand exits non-zero on the first failed check, with a message carrying GitHub's `::error::`
# prefix, and a release step after it does not run.
#
# ```bash
# bash packaging/release-steps.sh check-images
# bash packaging/release-steps.sh verify-tag <tag> <event-commit>
# bash packaging/release-steps.sh preflight
# bash packaging/release-steps.sh sign-files <file>...
# bash packaging/release-steps.sh build-rpm <output-directory> <render-nfpm.py arguments>...
# bash packaging/release-steps.sh sign-rpm <rpm>...
# bash packaging/release-steps.sh create-release <tag> <notes> <file>...
# bash packaging/release-steps.sh dispatch <tag>
# ```
#
# Environment, by subcommand (check-images reads none):
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
# names now, and a registry it could not read fails without naming one; a reference without a digest is refused. It does
# not read a secret, so it runs on a pull request from a fork.
#
# verify-tag fetches the tag from origin and holds it to an annotated tag object that names itself, peels to the event's
# commit and the checkout's HEAD, and carries a signature whose VALIDSIG primary is on the pinned list; the key is
# fetched from TAG_SIGNER_KEY_URL for that one check, so a leaked CI secret cannot mint a release by itself. It appends
# RELEASE_COMMIT and RELEASE_TAG_OBJECT to $GITHUB_ENV, and every later step names that object or that commit.
# create-release compares the remote's refs/tags/<tag> with RELEASE_TAG_OBJECT immediately before `gh release create
# --verify-tag`, which checks that the name exists, not which object it names.
#
# The signing key is imported into a keyring under /dev/shm, which must be tmpfs; a /dev/shm that is absent or
# not tmpfs refuses before a directory is created, and the EXIT trap stops the keyring's gpg-agent and removes it.
# preflight refuses an imported key whose primary is not ARTIFACT_SIGNER_PRIMARY_FINGERPRINT, and sign-files
# and sign-rpm verify against that primary's public key alone, so a signature by another key fails the step that made
# it. Every download is HTTPS alone and bounded in time, so a redirect to another scheme or a stalled server fails
# the step.
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
# container runs as root with `--cap-drop=all`, no new privileges, a read-only root, a bound on its processes and
# the rpm database on a tmpfs, since the step's environment on the runner holds the secrets; it mounts a directory
# holding the public key and the signed RPMs alone, which root reads without the capability that bypasses a directory's
# mode. The containers run through `sudo podman` with runc as the OCI runtime, which the GitHub runner needs: its
# default crun rejects the generated OCI spec. el-repos.sh points the EL9 container's repos at one ordered host list
# before its `dnf install`.
set -euo pipefail

NFPM_VERSION=2.47.0
NFPM_TARBALL_SHA256=0660ca602b2d2d2ae4781a06c692b3eeb9d437ffea05b831d76e41f4a3188783
# The multi-arch index digests of the Rocky 9 and 10 images, resolved from quay.io on 2026-10-08, and of the Fedora 44
# image, resolved on 2026-10-12. Every image is pinned by digest: EL9_IMAGE runs with the signing key and its output is
# published, and each verify image runs as root on the runner that holds the secrets; check-images refuses an entry
# without a digest. quay.io expires a Fedora digest once the tag moves on, so a release that reports `manifest unknown`
# for FC44_IMAGE takes the digest the tag resolves to now.
EL9_IMAGE=quay.io/rockylinux/rockylinux:9@sha256:8101994123cf3d0a8fee517bee7f39e555c7d92bd2d9eb3303cc988a0eeed00f
EL10_IMAGE=quay.io/rockylinux/rockylinux:10@sha256:827d37bc128288ccf160ee318bb3cb92d591164cb217e92f8bc61e3982ae1834
FC44_IMAGE=quay.io/fedora/fedora:44@sha256:1ff9eb02e4e2d7fb1175becac7139b2960c4fdcccedce81c86ec25b98ef3124d
# The distributions an RPM is built for, by dist tag, each with the image its signature is verified in.
declare -A DIST_IMAGES=([el9]="$EL9_IMAGE" [el10]="$EL10_IMAGE" [fc44]="$FC44_IMAGE")
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
_RELEASE_STEPS__ANSWER=""

_release_steps__die() { echo "::error::release-steps: $*" >&2; exit 1; }

# Every scratch directory and the signing container are removed on exit, a gpg-agent started in a scratch directory is
# stopped first, and INT and TERM exit through the same trap. The helpers set globals rather than print, since state
# set inside a command substitution's subshell does not reach this shell's trap.
scratch_directories=()
container=""
_RELEASE_STEPS__TEMP_DIR=""
_release_steps__remove_scratch_and_container() {
    local directory
    [[ -n "$container" ]] && sudo podman rm -f "$container" >/dev/null 2>&1
    for directory in "${scratch_directories[@]}"; do
        GNUPGHOME="$directory" gpgconf --kill gpg-agent 2>/dev/null || true
        rm -rf "$directory"
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
# set `signing_primary`.
_release_steps__import_signing_key() {
    _release_steps__create_scratch
    export GNUPGHOME="$scratch"
    printf '%s\n' "$GPG_SIGNING_KEY" | gpg --batch --quiet --import 2>/dev/null \
        || _release_steps__die "GPG_SIGNING_KEY did not import: it is not an ASCII-armored key block. Export the signing subkey with gpg --armor --export-secret-subkeys '<subkey-fpr>!'"
    gpg --batch --with-colons --list-secret-keys | grep -q '^sec:' \
        || _release_steps__die "GPG_SIGNING_KEY carries no secret key: it is a public key, or an export without the secret part"
    signing_primary="$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')"
}

_release_steps__verify_tag() {
    [[ "$#" -eq 2 ]] || _release_steps__die "usage: verify-tag <tag> <event-commit>"
    local tag="$1" event_commit="$2" tag_object embedded commit status primary
    _release_steps__require_env TAG_SIGNER_PRIMARY_FINGERPRINTS TAG_SIGNER_KEY_URL
    git fetch --quiet --no-tags --force origin "+refs/tags/$tag:refs/tags/$tag" \
        || _release_steps__die "refs/tags/$tag did not fetch from origin"
    tag_object="$(git rev-parse --verify --quiet "refs/tags/$tag^{tag}")" \
        || _release_steps__die "$tag is not an annotated tag; a release tag is created with git tag -s"
    embedded="$(git cat-file -p "$tag_object" | awk '/^tag / { print $2; exit }')"
    [[ "$embedded" == "$tag" ]] \
        || _release_steps__die "the tag object names itself $embedded, not $tag; a tag object is published under its own name"
    commit="$(git rev-parse "$tag_object^{commit}")"
    [[ "$commit" == "$event_commit" && "$commit" == "$(git rev-parse HEAD)" ]] \
        || _release_steps__die "$tag peels to $commit; the event's commit is $event_commit and the checkout is $(git rev-parse HEAD)"
    _release_steps__create_scratch
    export GNUPGHOME="$scratch"
    "${CURL[@]}" "$TAG_SIGNER_KEY_URL" | gpg --batch --quiet --import \
        || _release_steps__die "the tag signers' key did not import from $TAG_SIGNER_KEY_URL"
    status="$(git verify-tag --raw "$tag_object" 2>&1)" || _release_steps__die "$tag does not verify: $status"
    primary="$(printf '%s\n' "$status" | awk '/^\[GNUPG:\] VALIDSIG/ { print $NF; exit }')"
    [[ -n "$primary" && " $TAG_SIGNER_PRIMARY_FINGERPRINTS " == *" $primary "* ]] \
        || _release_steps__die "$tag is signed by primary ${primary:-none}, which is not on the pinned list: $TAG_SIGNER_PRIMARY_FINGERPRINTS"
    echo "$tag verified: tag object $tag_object, commit $commit, signed by $primary"
    if [[ -n "${GITHUB_ENV:-}" ]]; then
        printf 'RELEASE_COMMIT=%s\nRELEASE_TAG_OBJECT=%s\n' "$commit" "$tag_object" >> "$GITHUB_ENV"
    fi
}

_release_steps__run_preflight() {
    local response dist
    _release_steps__require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
    # sign-rpms.sh reads the passphrase as the first stdin line, so a second line would corrupt the key read after it.
    [[ "$GPG_SIGNING_PASSPHRASE" != *$'\n'* ]] || _release_steps__die "GPG_SIGNING_PASSPHRASE spans more than one line"
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
# names. On a failure it returns 1 with skopeo's message, on one line, in _RELEASE_STEPS__ANSWER.
_release_steps__read_manifest_digest() {
    local manifest="$_RELEASE_STEPS__TEMP_DIR/manifest" answer
    _RELEASE_STEPS__SERVED_DIGEST=""
    if answer="$(skopeo inspect --raw "docker://$1" 2>&1 >"$manifest")"; then
        _RELEASE_STEPS__SERVED_DIGEST="sha256:$(sha256sum <"$manifest" | cut -d' ' -f1)"
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

# check-images over this file's own pins, after proving that the text reader returns the DIST_IMAGES this shell holds,
# since the same reader reads the pins of another commit.
_release_steps__check_images() {
    [[ "$#" -eq 0 ]] || _release_steps__die "usage: check-images"
    local dist count
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
    [[ "$_RELEASE_STEPS__FAILED_COUNT" -eq 0 ]] \
        || _release_steps__die "check-images: $_RELEASE_STEPS__FAILED_COUNT of $count images failed the check"
    echo "release-steps: ok: every image resolves"
}

_release_steps__ensure_podman() {
    command -v podman >/dev/null || { sudo apt-get update -q && sudo apt-get install -y -q podman; } \
        || _release_steps__die "podman is not installed and did not install"
    sudo mkdir -p /etc/containers/containers.conf.d
    printf '[engine]\nruntime = "runc"\n' | sudo tee /etc/containers/containers.conf.d/10-runc.conf >/dev/null
    sudo podman info --format 'podman runtime: {{.Host.OCIRuntime.Name}} {{.Host.OCIRuntime.Version}}'
}

_release_steps__sign_rpm() {
    [[ "$#" -ge 1 ]] || _release_steps__die "usage: sign-rpm <rpm>..."
    local rpm name names=() work verify out image
    _release_steps__require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
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
        | sudo podman run -i --pull=never --name "$container" --network=none --entrypoint /usr/bin/bash -v "$work:/in:ro" \
            "$SIGN_IMAGE" -c '
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
        out="$(sudo podman run --rm --pull=never --network=none --cap-drop=all --security-opt=no-new-privileges --read-only \
                --pids-limit=64 --tmpfs /rpmdb --entrypoint /usr/bin/bash -v "$verify:/in:ro" "$image" -c '
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

[[ "$#" -ge 1 ]] || _release_steps__die "usage: release-steps.sh <check-images|verify-tag|preflight|sign-files|build-rpm|sign-rpm|create-release|dispatch> ..."
step="$1"
shift
case "$step" in
    check-images) _release_steps__check_images "$@" ;;
    verify-tag) _release_steps__verify_tag "$@" ;;
    preflight) _release_steps__run_preflight ;;
    sign-files) _release_steps__sign_files "$@" ;;
    build-rpm) _release_steps__build_rpm "$@" ;;
    sign-rpm) _release_steps__sign_rpm "$@" ;;
    create-release) _release_steps__create_release "$@" ;;
    dispatch) _release_steps__dispatch "$@" ;;
    *) _release_steps__die "unknown step $step" ;;
esac
