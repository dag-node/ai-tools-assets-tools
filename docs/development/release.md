# Branching and release

How a change to the tools reaches `develop`, becomes a signed tag on `main`
and an RPM on `rpm.dagnode.com`, and how an expired image digest is refreshed.

## The flow

```text
<type>/<id>-<name>, off develop
      |
      | pull request into develop, rebase-merged once validate is green
      v
   develop        every pull request and push: validate, tests (python 3.9),
      |           tests (python 3.12)
      |
      | chore/<id>-release-X.Y.Z: the changelog heading and the README
      | status, rebase-merged into develop like any other pull request
      v
   "Merge develop" pull request, develop -> main, merged with a merge commit
      |
      v
    main ---- git tag -s vX.Y.Z-rc.1, when the signing path changed
      |              |
      |              v
      |       release.yml: GitHub prerelease, no dispatch
      |
      | git tag -s vX.Y.Z on the same commit
      v
   release.yml: verify-tag, check-version, test-floor, the archive, sign-files,
   build-rpm, sign-rpm, create-release, dispatch
      |
      v
   GitHub release + rpm.dagnode.com
      |
      | git tag -f -s v1, once the run is green
      v
    v1 names the release
```

The rulesets of the repository decide the merge methods and who may tag. Each
row is read from `gh api repos/dag-node/ai-tools-assets-tools/rulesets`:

| Ref | Ruleset | What it does |
|---|---|---|
| `develop` | `develop-linear` | requires a linear history, so a pull request into `develop` is rebase-merged and a merge commit is refused |
| `develop`, `main` | `branches-no-delete` | refuses a deletion of either branch |
| `main` | `main-checks` | requires the checks `validate`, `tests (python 3.9)` and `tests (python 3.12)`, and refuses a deletion and a force push |
| `main` | `main-pr-required` | requires a pull request with one approving review from a code owner, merged with a merge commit alone |
| `v*.*.*` | `version-tags-create`, `version-tags-immutable` | restricts creation to the ruleset's bypass actors, and refuses an update, a deletion and a force push |
| `v1` | `major-tag-move`, `major-tag-no-delete` | restricts creation and update to the bypass actors, and refuses a deletion |

A release tag is immutable, so a release whose run failed is not re-tagged:
the fix lands on `develop` and the next patch version carries it. The release
run's `check-version` stops a tag before the first signature when the tagged
commit's `CHANGELOG.md` opens with another version, when the tag is not newer
than every release, or when another final release already names the commit.
A tag pushed on `main` before the `Merge develop` pull request is merged
therefore stops there. A final tag may name its release candidate's commit
when that candidate is the newest one. A release is a tag with a GitHub
release, so a tag whose run stopped before `create-release` does not hold back
a later tag; its own version is reused only after the tag is deleted
through the ruleset's bypass.

## Cutting a release

The version follows [Semantic Versioning](https://semver.org/),
which `CHANGELOG.md` names: an `[Unreleased]` section holding `### Fixed` alone
is a patch, and a new command, option or workflow input is a minor.

1. **The release branch** carries the version. Off a synced `develop`:

   ```bash
   git fetch origin && git switch -c chore/ATR-261103b-release-1.6.0 origin/develop
   ```

   In `CHANGELOG.md`, `## [1.6.0] - 2026-11-03` goes under an empty
   `## [Unreleased]`, and the README status names `v1.6.0`. Commit
   with `git commit -s` as `chore: release 1.6.0`, push, and open the pull
   request with that title. It is rebase-merged into `develop` once `validate`
   is green.

2. **`check-images`** runs at the commit the tag will name, immediately
   before the tag, from a checkout of that commit:

   ```bash
   bash packaging/release-steps.sh check-images
   ```

   A digest the registry retired is refreshed first, as [The digest
   refresh](#the-digest-refresh) states; a release run pulls every image
   before its first signature, so an expired digest stops the run there.

3. **`Merge develop`** is opened only once the release commit is on `develop`:
   opened earlier, it runs CI on a tree without that commit and again when it
   lands. Its body lists the commits `main` lacks, oldest first,
   which `origin/main..origin/develop` is exactly, since `main` is a merge
   of `develop`:

   ```bash
   git fetch origin
   gh pr create --repo dag-node/ai-tools-assets-tools --base main --head develop --title "Merge develop" --body "$(git log --reverse --format='- **%s**' origin/main..origin/develop)"
   gh pr merge <number> --repo dag-node/ai-tools-assets-tools --merge --admin
   ```

   `main-pr-required` asks for one approving review from a code owner,
   and `CODEOWNERS` names the maintainers team, so a maintainer's own pull
   request shows a grey merge button beside green checks: the author cannot
   approve it. The merge goes through the ruleset's bypass, which both routes
   record as the pull request's merge: `gh pr merge --merge --admin`
   from the terminal, or the merge button in the GitHub web interface. A local
   `git merge` and push is refused by the ruleset.

4. **A release candidate first, when the signing path changed.** Only a release
   run executes `sign-rpms.sh`, the signing subcommands of `release-steps.sh`,
   `release.yml` and `set-release.yml`, and a tag is immutable, so a release
   whose range touches one of them is tagged `vX.Y.Z-rc.1` first. The rc
   publishes a prerelease and skips the dispatch; the final tag names the rc's
   commit once the rc's run is green.

   ```bash
   git fetch origin
   git tag -s v1.6.0-rc.1 -m v1.6.0-rc.1 origin/main && git push origin v1.6.0-rc.1
   ```

5. **The final tag**, on `main`'s merge commit, or on the rc's commit:

   ```bash
   git tag -s v1.6.0 -m v1.6.0 'v1.6.0-rc.1^{}' && git push origin v1.6.0
   ```

   The push prints `Bypassed rule violations` for a bypass actor
   of `version-tags-create`, which is expected. The release run verifies
   the tag against the maintainer key `release.yml` pins, runs the suite
   at the Python floor, signs the archive and the RPMs, creates the release
   with the archive, its `.sha256` and `.asc` and the `el9`, `el10` and `fc44`
   RPMs, and asks `dag-node/rpm` to publish them. The release is published
   when each RPM answers at its tree,
   as `https://rpm.dagnode.com/el/9/x86_64/ai-tools-assets-tools-1.6.0-1.el9.noarch.rpm`
   does for EL9 (`el/10` and `fedora/44` for the others); the publish run lags
   the dispatch by minutes.

6. **`v1` moves** to the release once the final's run is green, so it never
   names a commit without a release; `major-tag-move` restricts the move to its
   bypass actors:

   ```bash
   git tag -f -s v1 -m v1 'v1.6.0^{}' && git push -f origin v1
   ```

7. **Downstream.** A caller re-pins its workflows to the release
   with `repin-tools` from its own checkout, as [Re-pinning a caller
   to a release](#re-pinning-a-caller-to-a-release) states, and `ai-tools-base`
   names the release in its `tools/lib/assets-tools.pin` before its next tag.

## The digest refresh

Every image a release runs is pinned by digest in `packaging/release-steps.sh`,
and `check-images` runs on every pull request and push here and in each caller.
A registry can retire a digest once its tag moves on; from that moment every
pull request fails at that step until both halves of the refresh are merged,
so the refresh is done at once, in this order: the tools first, since
a caller's half pins the tools commit. Open pull requests are not a reason
to wait: each passes on a re-run once the refresh is on `develop`,
and the commands list them and say so.

A refresh is a commit on `develop`, not a release: it changes the expired pin
lines and `CHANGELOG.md` alone, and the next release carries it. Two commands
carry it, each running under your own `git` and `gh` credentials, so push
rights are the permission and each merge stays yours. `--help` prints their
usage.

### Names

One id for both halves, `<PREFIX>-<yyMMdd><letter>` as every branch here
carries (`ATR-261103a`), given to each command with `--id`; the letter is
the next free one that day. `<dists>` is the dist tags of the refreshed images
in `DIST_IMAGES` order, joined with `-` (`fc44`, `el9-fc44`), and `<Distro>` is
the name each takes in prose (`Fedora 44`; `Rocky 9 and Fedora 44`).

| | the tools | each caller |
|---|---|---|
| command | `release-steps.sh refresh-images --id <id>` | `release-steps.sh repin-tools --id <id> <sha>` |
| branch | `build/<id>-<dists>-digest` | `ci/<id>-tools-<dists>-digest` |
| commit and pull request title | `build(packaging): pin the <Distro> image <registry> serves` | `ci: call the ai-tools-assets-tools commit that pins the <Distro> image` |
| changelog | `[Unreleased]`, `### Fixed`, one entry | none: a pin is not a change of the set |
| proof | the pull request's `validate` run, `check-images` green | the pull request's `<job> / validate` run green through the new pin |

The commit body and the pull request body are one text the command generates
from the check's output, the date and the repository's state.

### Half 1: the tools

In a checkout of the tools on `develop`, clean and equal to `origin/develop`:

```bash
bash packaging/release-steps.sh refresh-images --id ATR-261103a --caller ../ai-tools-assets
```

Each `--caller` names a caller's checkout, in which the command runs
`repin-tools`' preconditions first, so a caller that is not ready refuses
the refresh before this tree changes. What it does, in order, stopping
at the first refusal:

1. Refuses a tree with uncommitted changes, a checkout not on `develop`,
   a `develop` that differs from `origin/develop` after a fetch, and a `gh`
   that is not logged in.
2. Runs `check-images`. When every image resolves it prints
   `release-steps: ok: every image resolves; nothing to refresh` and exits 0.
   When the registry could not be read it refuses, since an expiry cannot be
   told from an outage, and does not change the tree.
3. Rewrites each expired `<DIST>_IMAGE` line to the digest its tag resolves
   to, with the line's `# resolved <date>` comment set to today, and adds
   under `## [Unreleased]`, `### Fixed`, created when absent:

   ```markdown
   - **<Distro> image.** A release verifies its <Distro> RPM in the image
     <registry> serves for `<repo>:<tag>` on <date>. <registry> retired the
     digest <previous version> pinned, so `check-images` reported `manifest
     unknown` on every pull request.
   ```

4. Lists the open pull requests into `develop`, with the note that each passes
   on a re-run once the refresh is on `develop`.
5. Commits on `build/<id>-<dists>-digest` with `-s` under your git identity,
   pushes, and opens the pull request with the title [Names](#names) lists
   and this body:

   ```text
   <registry> retired the <Distro> digest the release steps pinned (`<old,
   12 hex>`), so `check-images` fails every pull request and push here and
   in each caller with `manifest unknown`. The steps pin the digest
   `<repo>:<tag>` resolves to on <date> (`<new, 12 hex>`).

   A caller re-pins its `uses:` lines to this commit once it is on
   `develop`: `release-steps.sh repin-tools --id <id> <sha>`. No release is
   cut for a refresh; the next release carries it.
   ```

6. Prints the step after the merge, as `release-steps: next, …` lines:
   the `git ls-remote` that reads `develop`'s new head, and the `repin-tools`
   line for each caller.

The pull request is rebase-merged into `develop`. **Checkpoint:**

```bash
git ls-remote https://github.com/dag-node/ai-tools-assets-tools.git refs/heads/develop
```

That SHA, in full, is what every caller pins.

### Half 2: each caller

In the caller's checkout on `develop`, clean and equal to `origin/develop`,
with the tools checked out beside it, since the command runs from the tools'
tree:

```bash
bash ../ai-tools-assets-tools/packaging/release-steps.sh repin-tools --id ATR-261103a <full sha>
```

What it does, in order:

1. The refusals of half 1, on the caller's checkout; and when every `uses:`
   line already pins the SHA, prints so and exits 0.
2. Fetches the tools' `origin/develop` and refuses a SHA that is not on it, has
   more than one parent, or whose diff against its parent touches a line other
   than a `<DIST>_IMAGE` pin, its comment, or `CHANGELOG.md`. It then resolves
   the commit the caller's `uses:` lines pin today, a SHA or a tag
   on the tools' origin, and holds the diff from it to the SHA to the same
   lines: where the move would also bring another change of the tools, it
   refuses, naming the file and line, and the caller re-pins through a release
   of the tools instead, with its tag.
3. Runs `check-images` from the tools' tree at that SHA, so the caller proves
   what it is about to pin.
4. Rewrites every `uses:` line naming a workflow
   of `dag-node/ai-tools-assets-tools` to `@<full sha>` with one comment,
   `# develop <date>, <dists> digest, ai-tools-assets-tools#<PR>`,
   where `<date>` is the newest `# resolved` date of the pins the move brings
   and `<PR>` is the pull request GitHub names for the commit (`--pr <number>`
   where it names none). It refuses when the lines did not all carry one
   and the same `@<ref>` before, or when their readable comments disagree.
5. Lists the caller's open pull requests, as half 1 does.
6. Commits on `ci/<id>-tools-<dists>-digest` with `-s`, pushes, and opens
   the pull request with this body:

   ```text
   The release and validate workflows called the <previous pin> commit, whose
   release steps pin a <Distro> image digest <registry> no longer serves, so
   `check-images` failed every pull request here with `manifest unknown`.
   They now call the ai-tools-assets-tools `develop` commit `<sha, 7 hex>`
   (dag-node/ai-tools-assets-tools#<PR>), which pins the digest `<repo>:<tag>`
   resolves to on <date>.

   Not a release of the tools: the next tools release carries the digest,
   and the next re-pin here names that release.
   ```

   `<previous pin>` is the old comment's text (`v1.5.0`,
   or `develop <date>, …`).

The pull request's `validate` run is the proof: `check-images` passes
through the new pin, and the pins-agree check passes. Rebase-merge it.
**Checkpoint:** the caller's `develop` carries the commit, read
with `git ls-remote` on the caller's `refs/heads/develop`, and its next pull
request starts green.

### Re-pinning a caller to a release

The same command, given a tag, re-pins a caller to a release of the tools.
From the caller's root, with the tools checked out at `develop` beside it,
after the final's release run is green:

```bash
bash ../ai-tools-assets-tools/packaging/release-steps.sh repin-tools --id ATR-261120a v1.6.0
```

It refuses a prerelease tag, a tag that is not on the tools' origin, a tag
that is not annotated and signed by a key `release.yml` pins for `verify-tag`,
a tag whose `CHANGELOG.md` does not open its newest release with that version,
and a tag whose GitHub release is a prerelease or a draft or whose release run
did not conclude `success`. It then runs `check-images` at the tag's commit,
rewrites every `uses:` line to `@<full sha> # v1.6.0`, lists the open pull
requests, commits on `ci/<id>-tools-1.6.0`, pushes, and opens the pull request
`ci: call the ai-tools-assets-tools 1.6.0 workflows` with this body:

```text
The release and validate workflows previously called the <previous pin>
commit. They now call the 1.6.0 commit `<sha, 7 hex>` (release `v1.6.0`):

- <changelog headline 1>
- <changelog headline 2>
```

The headlines are the bold labels under the tools' `## [1.6.0]` section,
in order. The tools' `develop`, not the tag, is what the command runs
from: a fix to the command reaches a caller through `develop` without
a release, while the caller's workflows pin the release. Where the caller's git
signs commits, `gpg-agent` asks for the passphrase.

### If it stops midway

The two halves are not one transaction: the tools' pull request is
rebase-merged, which rewrites the commit, so the SHA a caller pins exists only
after the merge. Each command reads where it got to from the repositories,
so after an interruption the same line continues: a branch it already made is
reused, with the date it committed; a pushed branch is not pushed again;
an open pull request is printed instead of created; and pins already
at the target are reported as done. The commit is made before the branch is
created, so a commit that fails does not leave a branch behind, and a branch
of the same name carrying another change is refused by name.

Each step prints `release-steps: <command>: <doing>` before it runs
and `release-steps: ok: <checkpoint>` after it, so an interrupted run shows
the step it stopped in, and a finished run ends with the branch tip, the pull
request URL and the next command as `release-steps: next, …` lines.
Where GitHub answers late, as for the pull request right after a push,
the command retries with a backoff and then refuses naming the last answer; it
does not wait for a merge, which is your step. A refusal is one line naming
what the command found and the command to run next, before anything is pushed:

```text
::error::release-steps: the tree has uncommitted changes (`git status --short` printed above); commit or stash them, then run again
::error::release-steps: check-images could not read every image (the lines above), so an expiry cannot be told from an outage: retry later, nothing was changed
::error::release-steps: no pull request names commit 3f9c2a1 (`gh api repos/dag-node/ai-tools-assets-tools/commits/3f9c2a1/pulls` answered: …); pass it with `--pr <number>`
```

`--dry-run` runs every check, prints the diff, the title and the body, and does
not commit, push, open a pull request, or change a branch, a tag or the working
tree.
