#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Points every Rocky repo of the container it runs in at one ordered baseurl list instead of its mirrorlist, which picks
# a host per repo, so BaseOS and AppStream come from one snapshot and a depsolve across them holds; release-steps.sh runs
# it before `dnf install`. The CDN comes first; the rest are tried in order when a host is unreachable. The first
# expression uncomments Rocky's `#baseurl=` line, the second comments its mirrorlist; other repo files match neither.
# No package the release installs comes from `extras`, so it is disabled and a failed refresh of it cannot abort the
# install. The host list is ai-tools-base's (packaging/RpmBase.Containerfile).
set -euo pipefail

# shellcheck disable=SC2016 # $contentdir is dnf's variable, written into the repo file as text
sed -i \
    -e 's|^#baseurl=http://dl.rockylinux.org/\$contentdir/\(.*\)|baseurl=https://dl.rockylinux.org/$contentdir/\1 https://rocky-linux-us-central1.production.gcp.mirrors.ctrliq.cloud/pub/rocky/\1 https://ftp.sh.cvut.cz/rocky/\1 https://rockylinux.anexia.at/\1 https://ftp.fau.de/rockylinux/\1|' \
    -e 's|^mirrorlist=https://mirrors.rockylinux.org/|#&|' \
    /etc/yum.repos.d/*.repo
sed -i '/^\[extras\]/,/^\[/ s/^enabled=1$/enabled=0/' /etc/yum.repos.d/*.repo
