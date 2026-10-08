#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# The steps of a signed release, one subcommand per workflow step, shared by this repository's release.yml and the
# reusable set-release.yml a publisher calls, so the tag check, the key handling, the RPM signing and the dispatch exist
# in one copy. Each subcommand exits non-zero on the first failed check, with a message carrying GitHub's `::error::`
# prefix, and a release step after it does not run.
#
# ```bash
# bash packaging/release-steps.sh verify-tag <tag> <event-commit>
# bash packaging/release-steps.sh preflight
# bash packaging/release-steps.sh sign-files <file>...
# bash packaging/release-steps.sh build-rpm <nfpm-config> <output-directory>
# bash packaging/release-steps.sh sign-rpm <rpm>
# bash packaging/release-steps.sh create-release <tag> <notes> <file>...
# bash packaging/release-steps.sh dispatch <tag>
# ```
#
# Environment, by subcommand:
#   verify-tag      TAG_SIGNER_PRIMARY_FINGERPRINTS (space-separated), TAG_SIGNER_KEY_URL
#   preflight       GPG_SIGNING_KEY, GPG_SIGNING_PASSPHRASE, ARTIFACT_SIGNER_PRIMARY_FINGERPRINT; DISPATCH_RPM=true adds
#                   RPM_REPO_DISPATCH_TOKEN
#   sign-files      GPG_SIGNING_KEY, GPG_SIGNING_PASSPHRASE, ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
#   build-rpm       SOURCE_DATE_EPOCH
#   sign-rpm        GPG_SIGNING_KEY, GPG_SIGNING_PASSPHRASE, ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
#   create-release  GH_TOKEN, RELEASE_TAG_OBJECT (written by verify-tag)
#   dispatch        RPM_REPO_DISPATCH_TOKEN
#
# verify-tag fetches the tag from origin and holds it to an annotated tag object that names itself, peels to the event's
# commit and the checkout's HEAD, and carries a signature whose VALIDSIG primary is on the pinned list; the key is
# fetched from TAG_SIGNER_KEY_URL for that one check, so a leaked CI secret cannot mint a release by itself. It appends
# RELEASE_COMMIT and RELEASE_TAG_OBJECT to $GITHUB_ENV, and every later step names that object or that commit.
# create-release compares the remote's refs/tags/<tag> with RELEASE_TAG_OBJECT immediately before `gh release create
# --verify-tag`, which checks that the name exists, not which object it names.
#
# The signing key is imported into a keyring under /dev/shm, which must be tmpfs, and the EXIT
# trap stops its gpg-agent and removes it. preflight refuses an imported key whose primary is not
# ARTIFACT_SIGNER_PRIMARY_FINGERPRINT, and sign-files and sign-rpm verify against that primary's public key alone, so
# a signature by another key fails the step that made it.
#
# build-rpm runs nFPM at NFPM_VERSION and NFPM_TARBALL_SHA256, downloaded and checked before it runs, and builds the RPM
# without a signature; SOURCE_DATE_EPOCH fixes every timestamp nFPM writes. sign-rpm installs rpm-sign and gnupg2 in an
# EL9 container (the older rpm) that does not receive the secrets, so no package scriptlet runs while the key is
# present, signs the RPM with sign-rpms.sh beside this file in a container of that image, copies the signed file out
# with `podman cp`, and requires `rpmkeys -Kv` to print a signature line ending in OK inside the EL9 and the EL10
# container; the exit status alone passes an unsigned package. The secrets reach a container on stdin, since podman
# records an `-e` value in the container's configuration on disk. The containers run through `sudo podman` with runc as
# the OCI runtime, which the GitHub runner needs: its default crun rejects the generated OCI spec. el-repos.sh points
# the EL9 container's repos at one ordered host list before its `dnf install`.
set -euo pipefail

NFPM_VERSION=2.47.0
NFPM_TARBALL_SHA256=0660ca602b2d2d2ae4781a06c692b3eeb9d437ffea05b831d76e41f4a3188783
EL9_IMAGE=quay.io/rockylinux/rockylinux:9
EL10_IMAGE=quay.io/rockylinux/rockylinux:10
SIGN_IMAGE=localhost/ai-tools-assets-sign:el9
PACKAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# One pattern for the line rpm 4 and rpm 6 print for a valid signature; a digest-only line does not match.
SIGNATURE_OK_PATTERN='signature.*:[[:space:]]*OK[[:space:]]*$'

die() { echo "::error::release-steps: $*" >&2; exit 1; }

# Every scratch directory and the signing container are removed on exit, a gpg-agent started in a scratch directory is
# stopped first, and INT and TERM exit through the same trap. The helpers set globals rather than print, since state
# set inside a command substitution's subshell does not reach this shell's trap.
scratch_directories=()
container=""
cleanup() {
    local directory
    [[ -n "$container" ]] && sudo podman rm -f "$container" >/dev/null 2>&1
    for directory in "${scratch_directories[@]}"; do
        GNUPGHOME="$directory" gpgconf --kill gpg-agent 2>/dev/null || true
        rm -rf "$directory"
    done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# new_scratch: set `scratch` to a fresh directory under /dev/shm; a /dev/shm that is absent or not tmpfs refuses, so
# a key, a passphrase or a token does not reach a disk-backed directory.
new_scratch() {
    scratch="$(mktemp -d -p /dev/shm)" || die "no directory could be created under /dev/shm"
    scratch_directories+=("$scratch")
    [[ "$(stat -f -c %T "$scratch")" == tmpfs ]] || die "/dev/shm is not tmpfs"
}

require_env() {
    local name
    for name in "$@"; do
        [[ -n "${!name:-}" ]] || die "$name is empty; in CI it is $(env_hint "$name")"
    done
}

env_hint() {
    case "$1" in
        GPG_SIGNING_KEY) echo "the signing-subkey export, a secret of the release environment: gh secret set GPG_SIGNING_KEY --env release < signing-subkey.asc" ;;
        GPG_SIGNING_PASSPHRASE) echo "the signing subkey's passphrase, a secret of the release environment: gh secret set GPG_SIGNING_PASSPHRASE --env release" ;;
        RPM_REPO_DISPATCH_TOKEN) echo "a fine-grained token with contents: write on dag-node/rpm, a secret of the release environment: gh secret set RPM_REPO_DISPATCH_TOKEN --env release" ;;
        *) echo "set by the workflow" ;;
    esac
}

# import_signing_key: import GPG_SIGNING_KEY into a fresh scratch keyring, export GNUPGHOME, set `signing_primary`.
import_signing_key() {
    new_scratch
    export GNUPGHOME="$scratch"
    printf '%s\n' "$GPG_SIGNING_KEY" | gpg --batch --quiet --import 2>/dev/null \
        || die "GPG_SIGNING_KEY did not import: it is not an ASCII-armored key block. Export the signing subkey with gpg --armor --export-secret-subkeys '<subkey-fpr>!'"
    gpg --batch --with-colons --list-secret-keys | grep -q '^sec:' \
        || die "GPG_SIGNING_KEY carries no secret key: it is a public key, or an export without the secret part"
    signing_primary="$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')"
}

verify_tag() {
    [[ "$#" -eq 2 ]] || die "usage: verify-tag <tag> <event-commit>"
    local tag="$1" event_commit="$2" tag_object embedded commit status primary
    require_env TAG_SIGNER_PRIMARY_FINGERPRINTS TAG_SIGNER_KEY_URL
    git fetch --quiet --no-tags --force origin "+refs/tags/$tag:refs/tags/$tag" \
        || die "refs/tags/$tag did not fetch from origin"
    tag_object="$(git rev-parse --verify --quiet "refs/tags/$tag^{tag}")" \
        || die "$tag is not an annotated tag; a release tag is created with git tag -s"
    embedded="$(git cat-file -p "$tag_object" | awk '/^tag / { print $2; exit }')"
    [[ "$embedded" == "$tag" ]] \
        || die "the tag object names itself $embedded, not $tag; a tag object is published under its own name"
    commit="$(git rev-parse "$tag_object^{commit}")"
    [[ "$commit" == "$event_commit" && "$commit" == "$(git rev-parse HEAD)" ]] \
        || die "$tag peels to $commit; the event's commit is $event_commit and the checkout is $(git rev-parse HEAD)"
    new_scratch
    export GNUPGHOME="$scratch"
    curl -sSf "$TAG_SIGNER_KEY_URL" | gpg --batch --quiet --import \
        || die "the tag signers' key did not import from $TAG_SIGNER_KEY_URL"
    status="$(git verify-tag --raw "$tag_object" 2>&1)" || die "$tag does not verify: $status"
    primary="$(printf '%s\n' "$status" | awk '/^\[GNUPG:\] VALIDSIG/ { print $NF; exit }')"
    [[ -n "$primary" && " $TAG_SIGNER_PRIMARY_FINGERPRINTS " == *" $primary "* ]] \
        || die "$tag is signed by primary ${primary:-none}, which is not on the pinned list: $TAG_SIGNER_PRIMARY_FINGERPRINTS"
    echo "$tag verified: tag object $tag_object, commit $commit, signed by $primary"
    if [[ -n "${GITHUB_ENV:-}" ]]; then
        printf 'RELEASE_COMMIT=%s\nRELEASE_TAG_OBJECT=%s\n' "$commit" "$tag_object" >> "$GITHUB_ENV"
    fi
}

preflight() {
    local response
    require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
    # sign-rpms.sh reads the passphrase as the first stdin line, so a second line would corrupt the key read after it.
    [[ "$GPG_SIGNING_PASSPHRASE" != *$'\n'* ]] || die "GPG_SIGNING_PASSPHRASE spans more than one line"
    import_signing_key
    echo "imported signing key: primary $signing_primary"
    [[ "$signing_primary" == "$ARTIFACT_SIGNER_PRIMARY_FINGERPRINT" ]] \
        || die "GPG_SIGNING_KEY is key $signing_primary, not the pinned artifact signer $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT; a different primary is a different key, not a rotation"
    if [[ "${DISPATCH_RPM:-false}" == true ]]; then
        require_env RPM_REPO_DISPATCH_TOKEN
        response="$(GH_TOKEN="$RPM_REPO_DISPATCH_TOKEN" gh api repos/dag-node/rpm --jq .full_name 2>&1)" \
            || die "RPM_REPO_DISPATCH_TOKEN cannot read dag-node/rpm: $response"
        echo "the dispatch token reads $response"
    fi
}

# export_artifact_signer <file>: write the pinned primary's public key, from the imported secret, to <file>.
export_artifact_signer() {
    gpg --batch --armor --export "$ARTIFACT_SIGNER_PRIMARY_FINGERPRINT" > "$1"
    [[ -s "$1" ]] || die "the imported key does not hold the primary $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT"
}

sign_files() {
    [[ "$#" -ge 1 ]] || die "usage: sign-files <file>..."
    local file status primary
    require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
    import_signing_key
    for file in "$@"; do
        [[ -f "$file" ]] || die "$file is not a file"
        printf '%s' "$GPG_SIGNING_PASSPHRASE" | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 \
            --armor --detach-sign --output "$file.asc" "$file" \
            || die "gpg did not sign $file"
        status="$(gpg --batch --status-fd 1 --verify "$file.asc" "$file" 2>/dev/null)" || die "$file.asc does not verify"
        primary="$(printf '%s\n' "$status" | awk '/^\[GNUPG:\] VALIDSIG/ { print $NF; exit }')"
        [[ "$primary" == "$ARTIFACT_SIGNER_PRIMARY_FINGERPRINT" ]] \
            || die "$file.asc is signed by primary ${primary:-none}, not $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT"
        echo "$file.asc verified: signed by $primary"
    done
}

build_rpm() {
    [[ "$#" -eq 2 ]] || die "usage: build-rpm <nfpm-config> <output-directory>"
    local config="$1" output="$2" download tarball nfpm rpms
    require_env SOURCE_DATE_EPOCH
    [[ "$(uname -m)" == x86_64 ]] || die "the pinned nFPM checksum is for Linux x86_64, not $(uname -m)"
    [[ -f "$config" ]] || die "$config is not a file"
    mkdir -p "$output"
    compgen -G "$output/*.rpm" >/dev/null && die "$output already holds an RPM; build-rpm writes into an empty directory"
    new_scratch
    download="$scratch"
    tarball="nfpm_${NFPM_VERSION}_Linux_x86_64.tar.gz"
    curl -fsSL -o "$download/$tarball" "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/$tarball" \
        || die "nFPM $NFPM_VERSION did not download"
    echo "$NFPM_TARBALL_SHA256  $download/$tarball" | sha256sum --check --quiet \
        || die "$tarball does not match the pinned SHA-256 $NFPM_TARBALL_SHA256"
    tar -xzf "$download/$tarball" -C "$download" || die "$tarball did not extract"
    nfpm="$download/nfpm"
    [[ -f "$nfpm" ]] || die "$tarball holds no nfpm binary at its top level"
    "$nfpm" package --config "$config" --packager rpm --target "$output/" </dev/null \
        || die "nFPM did not build the RPM from $config"
    rpms=("$output"/*.rpm)
    [[ "${#rpms[@]}" -eq 1 && -f "${rpms[0]}" ]] || die "nFPM wrote ${#rpms[@]} files matching $output/*.rpm, not one"
    echo "built ${rpms[0]} with nFPM $NFPM_VERSION, SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
}

ensure_podman() {
    command -v podman >/dev/null || { sudo apt-get update -q && sudo apt-get install -y -q podman; } \
        || die "podman is not installed and did not install"
    sudo mkdir -p /etc/containers/containers.conf.d
    printf '[engine]\nruntime = "runc"\n' | sudo tee /etc/containers/containers.conf.d/10-runc.conf >/dev/null
    sudo podman info --format 'podman runtime: {{.Host.OCIRuntime.Name}} {{.Host.OCIRuntime.Version}}'
}

sign_rpm() {
    [[ "$#" -eq 1 ]] || die "usage: sign-rpm <rpm>"
    local rpm="$1" name work out image
    require_env GPG_SIGNING_KEY GPG_SIGNING_PASSPHRASE ARTIFACT_SIGNER_PRIMARY_FINGERPRINT
    [[ -f "$rpm" ]] || die "$rpm is not a file"
    name="$(basename "$rpm")"
    ensure_podman
    new_scratch
    work="$scratch"
    cp "$rpm" "$work/$name"
    cp "$PACKAGING/sign-rpms.sh" "$PACKAGING/el-repos.sh" "$work/"

    container="ai-tools-assets-sign-tools-$$"
    sudo podman run --name "$container" --entrypoint /usr/bin/bash -v "$work:/in:ro" "$EL9_IMAGE" -c '
            set -euo pipefail
            bash /in/el-repos.sh
            dnf -y -q --setopt=install_weak_deps=False install rpm-sign gnupg2' </dev/null \
        || die "rpm-sign and gnupg2 did not install in $EL9_IMAGE"
    sudo podman commit --quiet "$container" "$SIGN_IMAGE" >/dev/null
    sudo podman rm "$container" >/dev/null

    container="ai-tools-assets-sign-$$"
    out="$(printf '%s\n%s\n' "$GPG_SIGNING_PASSPHRASE" "$GPG_SIGNING_KEY" \
        | sudo podman run -i --name "$container" --entrypoint /usr/bin/bash -v "$work:/in:ro" "$SIGN_IMAGE" -c '
            set -euo pipefail
            mkdir /out && cp "/in/$1" /out/
            bash /in/sign-rpms.sh --secrets-stdin /out/sign-rpms.pub "/out/$1"' _ "$name")" \
        || die "sign-rpms.sh failed in $SIGN_IMAGE: $out"
    printf '%s\n' "$out"
    grep -q 'signed and verified' <<<"$out" || die "sign-rpms.sh did not run in $SIGN_IMAGE"
    sudo podman cp "$container:/out/$name" "$work/$name"
    sudo podman rm "$container" >/dev/null
    container=""
    sudo chown "$(id -u):$(id -g)" "$work/$name"

    import_signing_key
    export_artifact_signer "$work/artifact-signer.asc"
    for image in "$EL9_IMAGE" "$EL10_IMAGE"; do
        out="$(sudo podman run --rm --entrypoint /usr/bin/bash -v "$work:/in:ro" "$image" -c '
                db="$(mktemp -d)"
                rpmkeys --dbpath "$db" --import /in/artifact-signer.asc || exit 1
                rpm --version
                rpmkeys --dbpath "$db" -Kv "/in/$1" 2>&1' _ "$name" 2>&1)" \
            || die "rpmkeys -Kv refused $name in $image: $out"
        printf '%s:\n%s\n' "$image" "$out"
        grep -Eqi "$SIGNATURE_OK_PATTERN" <<<"$out" \
            || die "$name carries no signature that verifies in $image against $ARTIFACT_SIGNER_PRIMARY_FINGERPRINT"
    done
    cp "$work/$name" "$rpm"
    echo "$rpm signed in $EL9_IMAGE and verified in $EL9_IMAGE and $EL10_IMAGE"
}

create_release() {
    [[ "$#" -ge 3 ]] || die "usage: create-release <tag> <notes> <file>..."
    local tag="$1" notes="$2" remote flags=()
    shift 2
    require_env GH_TOKEN RELEASE_TAG_OBJECT
    remote="$(git ls-remote origin "refs/tags/$tag" | awk -v ref="refs/tags/$tag" '$2 == ref { print $1 }')"
    [[ "$remote" == "$RELEASE_TAG_OBJECT" ]] \
        || die "refs/tags/$tag on the remote is ${remote:-absent}; the verified tag object is $RELEASE_TAG_OBJECT, so the tag moved and the release is not created"
    # The version is the part after the last slash (v1.2.3, or <set>/v1.2.3), so a hyphen in a set name is not read
    # as a prerelease.
    [[ "${tag##*/}" == *-* ]] && flags+=(--prerelease)
    gh release create "$tag" --verify-tag --title "$tag" --notes "$notes" "${flags[@]}" "$@"
}

dispatch() {
    [[ "$#" -eq 1 ]] || die "usage: dispatch <tag>"
    local tag="$1" attempts=5 attempt
    require_env RPM_REPO_DISPATCH_TOKEN GITHUB_REPOSITORY
    # preflight read dag-node/rpm with this token, so a failure here is transient and is retried with a backoff.
    for attempt in $(seq 1 "$attempts"); do
        if GH_TOKEN="$RPM_REPO_DISPATCH_TOKEN" gh api repos/dag-node/rpm/dispatches \
                -f event_type=publish-rpm \
                -F "client_payload[project]=$GITHUB_REPOSITORY" \
                -F "client_payload[tag]=$tag"; then
            echo "dispatched publish-rpm for $GITHUB_REPOSITORY $tag to dag-node/rpm"
            return 0
        fi
        [[ "$attempt" -lt "$attempts" ]] && sleep $((attempt * 10))
    done
    die "the dispatch to dag-node/rpm failed after $attempts attempts; the release exists, and re-running this step dispatches again"
}

[[ "$#" -ge 1 ]] || die "usage: release-steps.sh <verify-tag|preflight|sign-files|build-rpm|sign-rpm|create-release|dispatch> ..."
step="$1"
shift
case "$step" in
    verify-tag) verify_tag "$@" ;;
    preflight) preflight ;;
    sign-files) sign_files "$@" ;;
    build-rpm) build_rpm "$@" ;;
    sign-rpm) sign_rpm "$@" ;;
    create-release) create_release "$@" ;;
    dispatch) dispatch "$@" ;;
    *) die "unknown step $step" ;;
esac
