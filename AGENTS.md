# Working in this repository

What a coding agent reads before it changes this repository, and how it conducts a session in it.

- **[CONTRIBUTING.md](CONTRIBUTING.md), before the first commit.** It states what changes where, the checks CI runs,
  the commit style, the sign-off every commit carries (`git commit -s`, and no co-author trailer for a tool or a model,
  which cannot certify the DCO), the branch name `<type>/<id>-<name>` off `develop`, and the licence a contribution is
  made under.
- **[docs/development/release.md](docs/development/release.md)** is the release process: the flow from a branch
  to a signed tag, the rulesets that decide each merge method, the release checklist, and the digest refresh.

## A session's conduct

1. **Know the branch state first.** Run `git fetch origin`, then read the current branch, where `develop` and `main`
   stand against `origin/develop` and `origin/main`, and which local branches are merged or local only. A change is
   never started from a stale `develop`: the branch is cut from `origin/develop` after the fetch.
2. **Branch from the right base.** A branch is cut from `origin/develop`, and `main`, the last release, is not a base;
   a branch stacked on another is cut from the branch it stacks on and waits for that branch's merge.
3. **Keep a merge map until it is achieved.** One ordered table for the session: branch, pull request base, title
   in the `type(scope): summary` form, merge method (rebase-merge into `develop`; a merge commit for `develop` →
   `main`), and what the row waits on (the pull request it stacks on, a release, a tag). The map is carried
   in the session's closing message and updated as rows land.
4. **Hand over each outward step, one at a time.** A push, a pull request, a merge, a tag and a move of `v1` are
   the maintainer's: the session hands each one over as a runnable command with the check that verifies it
   (`git ls-remote`, the pull request's checks, the release page), then stops until the result is read.
5. **The checkout is shared.** A step the maintainer runs (`git switch develop`, `repin-tools`) moves the checkout
   the session works in, so the session reads `git branch --show-current` before a commit or a rebase that follows
   a handed-over step.
