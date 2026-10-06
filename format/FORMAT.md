# The asset format, format 1

What a set of skills and subagents holds, how its files are named and written,
and the rule `tools/validate` applies to each. A set that passes installs
as a Claude Code or Codex plugin as committed, and `ai-tools-base` reads it
as a package; base's own validator applies the rows marked as enforced by base,
and the conformance fixtures under `fixtures/` hold the two to one another.

```text
sets/<set>/
├── set.conf                    format, name, version, summary, licence
├── CHANGELOG.md
├── README.md
├── LICENSE                     optional: the set's own licence text
├── LICENSES/                   optional: texts of the identifiers the set declares
├── plugin.json                 Agent Plugins 1.0 manifest, generated
├── .claude-plugin/plugin.json  Claude Code manifest, generated
├── skills/<name>/
│   ├── SKILL.md
│   ├── scripts/                optional, called through an interpreter
│   ├── references/             optional, linked from SKILL.md
│   ├── assets/                 optional text templates and data
│   ├── tests/                  optional self-contained tests
│   └── UPSTREAM.conf           a vendored skill alone
├── agents/<name>.md            subagents, Claude Code's format
└── metadata/<kind>/<name>/     optional
    ├── asset.conf              requirements and targets
    ├── UPSTREAM.conf           provenance of a vendored flat-file asset
    └── references/
```

A set directory holds these entries and no other. `jobs/`, `libs/`
and `variants/` are reserved and hold no content; every other Claude Code
plugin component (hooks, MCP and LSP servers, `bin/`, commands, monitors,
workflows, output styles, themes, `settings.json`) is refused, and so is
a component kind Claude Code adds later, since the list is an allowlist.

## Profiles

The **source** profile is the committed tree, checked on every pull request.
The **release** profile is a set `tools/build-set` staged: the same tree plus
`LICENSES/` texts for every identifier it declares, the repository's `LICENSE`
where the set has none, and `SHA256SUMS` at the set root listing every file.
`SHA256SUMS` and its signature are not committed: the source profile refuses
them.

## Names

A set name and an asset name are 1 to 64 characters of `a-z`, `0-9` and `-`,
not starting or ending with `-`, without `--`, and without `anthropic`
or `claude`. A skill's directory, a subagent's file stem and the frontmatter
`name` are the asset name. An asset's id is `<set>/<kind>/<name>`.

A published set is named `<publisher>` or `<publisher>-<topic>`; `core` belongs
to dag-node. A set name does not start with a word
from `tools/reserved-words.txt`, the names of AI vendors, their agents
and models, and large technology companies. An authored asset carries its set's
prefix, `ai-tools-` in `core` and `<set>-` elsewhere, and `ai-tools-` is
refused in every other set. A vendored asset keeps its upstream name
and records its origin in `UPSTREAM.conf`. A skill and a subagent do not share
a name, since an agent lists both kinds in one list.

## Kinds

| Kind id | Directory | Shape | State |
|---|---|---|---|
| `skills` | `skills/` | a directory holding `SKILL.md` | implemented |
| `subagents` | `agents/` | a `<name>.md` file | implemented |
| `orientation` | — | base's own; not a set kind | base-only |
| `jobs`, `mcps`, `commands`, `instructions`, `hooks`, `lsps`, `output-styles`, `settings`, `workflows`, `themes`, `monitors`, `tools` | the kind's name | reserved: refused with content until a later format defines each | reserved |

The id `subagents` is stable; the directory is Claude Code's `agents/`,
and `agents/` holds subagent files alone because Claude Code loads every `.md`
file in it as a subagent.

## `set.conf`

`KEY=value` lines, read and not executed: one layer of quotes, `#` comments,
`[a, b]` lists, a key written once.

| Key | Required | Value |
|---|---|---|
| `format` | yes | `1` |
| `name` | yes | the set directory's name |
| `version` | yes | a semantic version |
| `summary` | yes | one line; the manifests' description |
| `license` | yes | an SPDX expression on the list in force |
| `maintainers` | yes | a list of contacts, the publisher's |
| `source` | yes | the repository the set is published from, the publisher's |
| `requires_base` | no | the least `ai-tools-base` version |
| `integrations` | no | a list of `integration-<name>` tokens the assets need |
| `requires_capabilities` | no | a list of capability tokens; an unknown one refuses the set |

The capabilities this format defines are `skills.portable.v1`
and `subagents.claude.v1`. An unknown key is reported and read past.

## The manifests and the marketplace

`plugin.json` and `.claude-plugin/plugin.json` carry the set's name
as the plugin `ai-tools-assets-<set>`, its `version`, its `summary`
as the description, its `license`, and the publisher's `publisher`
and `contact` as the author with `source` as homepage and repository. Neither
declares a component key. The repository's `.claude-plugin/marketplace.json`
lists every set the same way. `tools/sync-manifests` writes all three
from `set.conf` and `publisher.conf`, and validation refuses a committed copy
that differs.

`publisher.conf` at the repository root carries `publisher`, `contact`,
`maintainers`, `source` and `description` (the marketplace's), and optionally
`licenses`, the SPDX allowlist that replaces the default.

## Skills

The frontmatter is the Agent Skills specification's fields:

| Field | In a set skill |
|---|---|
| `name`, `description` | required strings; `description` at most 1024 characters |
| `license` | allowed; a string, the SPDX expression that overrides the set's licence for this skill |
| `compatibility` | allowed; a string of at most 500 characters |
| `metadata` | allowed; a map of string values; keys this format reads start with `ai-tools-` |
| `allowed-tools`, any other key | refused |

A skill's root holds `SKILL.md`, `scripts/`, `references/`, `assets/`,
`tests/`, `UPSTREAM.conf`, `LICENSE` and `LICENSES/` alone. It does not hold
a `.claude-plugin/` directory, which would make it a plugin of its own,
and `agents/openai.yaml` is reserved. `SKILL.md` under 500 lines is a warning.

A script is called through its interpreter and ships every file it runs.
A `.cs` script is a .NET file-based app: `#:package` is refused, `#:sdk` is
`Microsoft.NET.Sdk` or `Microsoft.NET.Sdk.Web`, and `#:project` names
a `.csproj` inside the skill.

## Subagents

An allowlisted subset of Claude Code's format:

| Field | In a set subagent |
|---|---|
| `name`, `description` | required strings; `description` at most 1024 characters |
| `model`, `effort`, `color` | allowed strings |
| `tools`, `disallowedTools`, `skills` | allowed string lists: a flow list `[Read, Grep]`, a block sequence of `- item` lines, or Claude Code's comma-separated line `Read, Grep` |
| `maxTurns` | allowed; an unquoted positive integer |
| `metadata` | allowed; a map of string values |
| `permissionMode`, `hooks`, `mcpServers` | refused: they change permissions, run commands or add tool servers |
| `memory`, `isolation`, `background`, `initialPrompt`, `omitClaudeMd`, `experimental`, any other key | refused until a set needs one and it is reviewed |

## Frontmatter syntax

The frontmatter opens with `---` on the first line and closes with the next
`---` line. Inside, a line is `key: value` at the margin, a blank or a `#`
comment. A value is a plain scalar, a quoted scalar on one line, a flow list
`[a, b]` of plain items, or empty with the lines indented under it forming
a one-level map of `key: value` lines or a sequence of `- item` lines. A tab
in the indentation, a block scalar, an anchor, an alias, a tag, a flow map,
a deeper nesting and a key written twice are refused, so every reader parses
the same file the same way.

Every field has a declared type, checked before its value. A plain scalar
that another YAML reader types -- `true`, `no`, `null`, `~`, an integer,
a float, in any letter case -- is refused in a string field: quote it. A plain
scalar does not carry `: ` or ` #`, which a reader takes as a mapping
or a comment; quote the value instead. A double-quoted scalar escapes `\\`
and `\"` alone, a single-quoted one `''` alone. A flow list holds plain items,
none empty and none opening with a YAML indicator.

## Bodies and files

- No line runs a command when the asset loads: `` !`command` `` at the start
  of a line or after whitespace, anywhere in a line, and a fence whose info
  string's first word carries `!` (` ```! `, ` ```bash! `) are refused
  in `SKILL.md` and in a subagent file. `scripts/` and `references/` are not
  scanned for them: a script runs through its interpreter, and a reference is
  read, not loaded.
- No `.md` file of an asset names an absolute path into `/opt/ai-tools`,
  `/usr/share` or `/usr/local/share`; a skill names its own files relative
  to its root and another skill by name.
- Every entry is a regular file or a directory: no symbolic link, no file
  with a second hard link, no special file. Every file is UTF-8 text without
  a control character (every `Cc` code point other than tab, LF and CR, the C1
  range included), a `Bidi_Control` code point or a byte order mark anywhere
  in it, at most 1 MiB; a set holds at most 2000 files
  and 500 directories, 32 levels deep, 64 MiB in all, with at most 2000 entries
  in one directory, and the validator stops at the first of these it meets.
  A file name is printable and does not carry a newline.
- A reserved name is used for its reserved purpose alone: `SHA256SUMS`,
  `SHA256SUMS.asc`, `SHA512SUMS*`, `*.oms.sig`, `UPSTREAM.conf`, `asset.conf`,
  `plugin.json`, a `README.md` at a kind directory, and the directories
  `.claude-plugin/`, `.agents/`, `metadata/` and `variants/`.

## Provenance and metadata

A vendored skill carries `UPSTREAM.conf` at its root; a vendored flat-file
asset carries it under `metadata/<kind>/<name>/`. One declaration per asset.

| Key | Required | Value |
|---|---|---|
| `source` | yes | the upstream repository URL |
| `path` | yes | the asset's path inside it |
| `revision` | yes | the full commit id the copy was taken from |
| `license` | yes | the upstream SPDX licence, on the list in force |
| `signature`, `signer` | no | reserved for asset signing |

`metadata/<kind>/<name>/asset.conf` carries `format=1` and optionally
`requires_capabilities`, `requires_integrations` (as `integration-<name>`)
and `targets` (agent names). `<kind>` is an implemented kind id and `<name>`
an asset the set holds. An unknown required capability refuses the asset.

## Licences

Every licence a set declares, in `set.conf`, a skill's `license` field
or an `UPSTREAM.conf`, is a well-formed SPDX expression whose every identifier,
under `AND` and under `OR` alike, is on the list in force. `WITH`,
`LicenseRef-`, a `+` suffix, `NONE` and `NOASSERTION` are refused. The default
list is `MIT`, `MIT-0`, `0BSD`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`,
`Apache-2.0`, `CC0-1.0` and `Unlicense`; `publisher.conf` `licenses=[...]`
replaces it, and an explicitly empty list refuses every licence. The text
of each identifier is `LICENSES/<identifier>.txt`, in the set
or at the repository root, and `build-set` copies it into the payload.

## Forward compatibility

Allowing a name, a field or an entry later is additive; refusing one a shipped
set carries breaks that set, which is why each list starts narrow. `format`
stays the integer `1` until a change a set must follow, which is `format=2`
and a new major of the tools. An unknown informational key in `set.conf`,
`UPSTREAM.conf` or `asset.conf` is reported and read past; a requirement
(`requires_base`, `requires_capabilities`, `requires_integrations`, `targets`)
is read, not skipped, and an unknown required capability refuses. An unknown
top-level set entry fails validation; a reserved one is refused with content.

## Rules

Every finding `tools/validate` prints carries one of these ids, and a fixture
under `fixtures/fail/<rule>/` fails on that rule alone. The table is
`python3 tools/validate --list-rules --markdown`; `tests/` fails when the two
differ.

<!-- rules:begin -->
| Rule | Outcome | Requires |
|---|---|---|
| `repo.layout` | refuses | the repository root holds `publisher.conf` and `sets/` |
| `repo.publisher-conf` | refuses | `publisher.conf` reads as `KEY=value` with every required key |
| `repo.marketplace` | refuses | `.claude-plugin/marketplace.json` equals what `sync-manifests` writes from `set.conf` and `publisher.conf` |
| `set.conf.missing` | refuses | a set directory holds `set.conf` |
| `set.conf.syntax` | refuses | `set.conf` reads as `KEY=value`: every line is `KEY=value` or a comment, a key is written once, a list is `[a, b]` |
| `set.conf.required-key` | refuses | `set.conf` carries `format`, `name`, `version`, `summary`, `license`, `maintainers` and `source` |
| `set.conf.format` | refuses | `format` is the integer `1` |
| `set.conf.name` | refuses | `name` equals the set directory |
| `set.conf.version` | refuses | `version` is a semantic version |
| `set.conf.requires-capabilities` | refuses | every required capability is one the format defines |
| `set.conf.integrations` | refuses | every integration is written as `integration-<name>` |
| `set.conf.unknown-key` | warns | an unknown key is reported; base reads past it |
| `set.entry.unknown` | refuses | a set directory holds `set.conf`, `CHANGELOG.md`, `README.md`, `LICENSE`, `LICENSES`, `plugin.json`, `.claude-plugin`, `skills`, `agents` and `metadata` alone |
| `set.entry.reserved` | refuses | `jobs`, `libs` and `variants` are reserved and do not hold any content |
| `set.manifest.plugin` | refuses | `plugin.json` and `.claude-plugin/plugin.json` carry the set's name, version, summary and licence |
| `set.manifest.components` | refuses | a plugin manifest does not declare a component key |
| `set.manifest.claude-plugin` | refuses | `.claude-plugin` holds `plugin.json` alone |
| `name.grammar` | refuses | a name is 1 to 64 characters of `a-z`, `0-9` and single hyphens, starting and ending with a letter or digit |
| `name.reserved-claude` | refuses | a name does not contain `anthropic` or `claude` |
| `name.reserved-word` | refuses | a set name does not start with a word from `reserved-words.txt` |
| `name.publisher` | refuses | a set is named `<publisher>` or `<publisher>-<topic>`; `core` belongs to dag-node |
| `name.asset-prefix` | refuses | an authored asset of `core` is named `ai-tools-<name>`; one of another set `<set>-<name>` |
| `name.frontmatter` | refuses | the frontmatter `name` equals the directory or file-stem name |
| `name.collision` | refuses | a skill and a subagent do not share a name |
| `name.composed-length` | warns | `<plugin>:<name>` is at most 64 characters, the OpenAI submission limit |
| `kind.shape` | refuses | an entry under `skills/` is a directory holding `SKILL.md`, and one under `agents/` a `.md` file |
| `kind.reserved` | refuses | a reserved kind directory does not hold any content |
| `skill.entry.unknown` | refuses | a skill holds `SKILL.md`, `scripts`, `references`, `assets`, `tests`, `UPSTREAM.conf`, `LICENSE` and `LICENSES` alone |
| `skill.plugin-manifest` | refuses | a skill does not hold a `.claude-plugin` directory |
| `skill.sidecar` | refuses | `agents/openai.yaml` is reserved inside a skill |
| `skill.length` | warns | `SKILL.md` is under 500 lines |
| `frontmatter.missing` | refuses | `SKILL.md` and a subagent file open with a frontmatter |
| `frontmatter.syntax` | refuses | the frontmatter is in the accepted YAML subset: a plain scalar carries no `: ` or ` #`, a double-quoted one escapes `\\` and `\"` alone, a flow list holds plain, non-empty items |
| `frontmatter.type` | refuses | a field has its declared type: a string is quoted where YAML would read a number, a boolean or null; `tools`, `disallowedTools` and `skills` are string lists; `maxTurns` is an unquoted integer; `metadata` values are strings |
| `frontmatter.required` | refuses | `name` and `description` are present and non-empty |
| `frontmatter.refused-key` | refuses | a frontmatter key is on the kind's allowlist |
| `frontmatter.length` | refuses | `description` is at most 1024 characters and `compatibility` at most 500 |
| `frontmatter.metadata` | refuses | `metadata` is a map of string values |
| `frontmatter.metadata-prefix` | warns | a `metadata` key this format reads starts with `ai-tools-` |
| `body.dynamic-injection` | refuses | a line does not run a command when the asset loads |
| `body.absolute-path` | refuses | a body does not name an absolute path into `/opt/ai-tools`, `/usr/share` or `/usr/local/share` |
| `file.symlink` | refuses | a set does not hold a symbolic link |
| `file.hardlink` | refuses | a file has one link |
| `file.special` | refuses | every entry is a regular file or a directory |
| `file.binary` | refuses | every file is UTF-8 text without a control character (C0, DEL, C1, other than tab, LF and CR), a bidi control or a byte order mark |
| `file.size` | refuses | a file is at most 1 MiB; a set holds at most 2000 files, 500 directories, 2000 entries in one directory, 32 levels and 64 MiB in all |
| `file.reserved-name` | refuses | a reserved file name is used for its reserved purpose alone |
| `file.name` | refuses | a file name is printable and does not carry a newline |
| `cs.package` | refuses | a `.cs` script does not carry a `#:package` directive |
| `cs.sdk` | refuses | a `.cs` script's `#:sdk` is `Microsoft.NET.Sdk` or `Microsoft.NET.Sdk.Web` |
| `cs.project` | refuses | a `.cs` script's `#:project` names a `.csproj` inside the skill |
| `provenance.syntax` | refuses | `UPSTREAM.conf` reads as `KEY=value` with `source`, `path`, `revision` and `license` |
| `provenance.duplicate` | refuses | an asset has one `UPSTREAM.conf`, in the skill or under `metadata` |
| `metadata.kind` | refuses | `metadata/<kind>` is an implemented kind id |
| `metadata.asset` | refuses | `metadata/<kind>/<name>` names an asset the set holds |
| `metadata.entry` | refuses | `metadata/<kind>/<name>` holds `asset.conf`, `UPSTREAM.conf` and `references` alone |
| `metadata.asset-conf` | refuses | `asset.conf` reads as `KEY=value` with `format=1` and known capabilities, integration tokens and target names |
| `license.expression` | refuses | a licence is a well-formed SPDX expression without `WITH`, `LicenseRef` or a `+` suffix |
| `license.allowlist` | refuses | every identifier of a licence is on the list in force |
| `license.text` | refuses | `LICENSES/<identifier>.txt` exists for every identifier a set declares |
| `license.file` | refuses | every file of a repository states its licence in an SPDX header or a `REUSE.toml` annotation |
| `release.inventory` | refuses | `SHA256SUMS` lists every file of the built set and matches each |
<!-- rules:end -->
