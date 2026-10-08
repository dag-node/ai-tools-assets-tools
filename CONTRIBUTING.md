# Contributing

What this repository holds, how a change is checked, and the conventions
for commits and pull requests. The format a set follows is `format/FORMAT.md`;
this file is about changing the tools that apply it.

## What changes where

| Change | Where |
|---|---|
| a rule a set must follow | `format/FORMAT.md` states it with its rule id, `tools/validate` applies it, and a fixture under `fixtures/fail/<rule>/` fails on it alone |
| a command | `tools/<command>`, with its tests under `tests/` |
| a shared Python module | `tools/lib/`, named with underscores, imported by the commands |
| a workflow a publisher calls | `.github/workflows/`, with its inputs documented in the workflow's header |
| a formatter | `formatters/`, under `AGPL-3.0-only` |
| a release step | `packaging/release-steps.sh`, which both release workflows run |

A tool is Python 3.9 and the standard library alone, or bash, so it runs
on an EL9 host and on the GitHub runner without an install step; a script is
called through its interpreter (`python3 tools/validate`), so a call does not
depend on the exec bit. A validator reads set content as data: it does not
import Python from a set, execute a set's scripts or source its configuration.

## Checks

CI runs the tests under `tests/`, which drive each command over the fixtures,
`shellcheck` over the shell files, `reuse lint`, and the SPDX allowlist check
every publisher repository runs. The same commands run from a checkout.

## Commit style

Commit messages follow `type(scope): summary` (`feat`, `fix`, `docs`, `test`,
`chore`, `refactor`), with the directory as the scope:
`feat(tools): refuse a symlink inside an asset`,
`docs(format): state the subagent frontmatter allowlist`. Keep the "why"
in the body. Record a user-visible change in `CHANGELOG.md` in the same pull
request.

### Sign-off

Every commit carries a `Signed-off-by: Name <email>` trailer that matches its
author; `git commit -s` adds it. The trailer certifies the [Developer
Certificate of Origin 1.1](https://developercertificate.org/): that you wrote
the change, or have the right to submit it, under the licence the file states.
It is a certification, not a cryptographic signature. The `validate` workflow
checks every commit of a pull request for an author-matching sign-off once
outside contributions open, with no exemption for a bot or a merge commit.
A `Co-Authored-By` trailer names a person who also signs off, so a commit does
not carry a co-author trailer for a tool or a model, which cannot certify
the DCO.

## Pull requests

Use a branch off `develop` named `<type>/<id>-<name>` and give the pull request
an explicit title in the same `type(scope): summary` form as a commit subject.
`main` receives releases: a release is tagged `v<semver>` on `main`,
and the `v1` tag moves to it.

## License

Contributions are made under the MIT license (see `LICENSE`) unless the file
states another licence in its SPDX header. The files under `formatters/` and
`packaging/sign-rpms.sh` are `AGPL-3.0-only`, and a contribution to one of them
is under that licence.
`REUSE.toml` covers a file that does not state one, and `LICENSES/` holds
the text of every identifier the repository uses.
