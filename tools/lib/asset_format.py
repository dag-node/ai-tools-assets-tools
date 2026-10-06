# SPDX-License-Identifier: MIT
"""The asset format, format 1, as data: the names, the kinds, the allowlists, the limits and the rule ids.

`format/FORMAT.md` states each rule for a publisher; this module is the one place the tools read it from, so
`tools/validate`, `tools/new-asset` and `tools/build-set` agree on what a set holds, and `tests/` holds the
specification and this registry to one another. Every constant here is a format decision: allowing a name, a field
or an entry later is additive, while refusing one a shipped set carries breaks that set, which is why each list starts
narrow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple

FORMAT_VERSION = 1

# ── Names ──────────────────────────────────────────────────────────────────────────────────────────────────────────
# The Agent Skills specification's `name` rule, applied to assets and sets alike: 1 to 64 characters of a-z, 0-9
# and single hyphens, starting and ending with a letter or digit.
NAME_PATTERN = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
NAME_MAX_LENGTH = 64
CLAUDE_RESERVED_WORDS: Tuple[str, ...] = ("anthropic", "claude")
UPSTREAM_PUBLISHER = "dag-node"
UPSTREAM_SET = "core"
BASE_SET = "ai-tools"
UPSTREAM_ASSET_PREFIX = "ai-tools-"
PLUGIN_NAME_PREFIX = "ai-tools-assets-"
MARKETPLACE_NAME_SUFFIX = "-ai-tools-assets"
# OpenAI's plugin submission limits `<plugin>:<skill>` to 64 characters; the tools warn, since it binds one route.
COMPOSED_NAME_MAX_LENGTH = 64
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


# ── Kinds ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class KindDefinition:
    """One row of the kind table: the stable kind id, the set directory it is read from, and its shape."""

    kind_id: str
    directory: str
    source_shape: str  # `directory-with-entry-file`, `flat-file` or `reserved`
    entry_file: str
    suffix: str
    support: str  # `implemented`, `reserved` or `base-only`

    @property
    def is_implemented(self) -> bool:
        return self.support == "implemented"

    @property
    def is_set_admissible(self) -> bool:
        return self.support != "base-only"


KINDS: Tuple[KindDefinition, ...] = (
    KindDefinition("skills", "skills", "directory-with-entry-file", "SKILL.md", "", "implemented"),
    KindDefinition("subagents", "agents", "flat-file", "", ".md", "implemented"),
    KindDefinition("orientation", "", "flat-file", "AGENTS.md", "", "base-only"),
    KindDefinition("jobs", "jobs", "reserved", "job.conf", "", "reserved"),
    KindDefinition("mcps", "mcps", "reserved", "", "", "reserved"),
    KindDefinition("commands", "commands", "reserved", "", ".md", "reserved"),
    KindDefinition("instructions", "instructions", "reserved", "", ".md", "reserved"),
    KindDefinition("hooks", "hooks", "reserved", "", "", "reserved"),
    KindDefinition("lsps", "lsps", "reserved", "", "", "reserved"),
    KindDefinition("output-styles", "output-styles", "reserved", "", ".md", "reserved"),
    KindDefinition("settings", "settings", "reserved", "", "", "reserved"),
    KindDefinition("workflows", "workflows", "reserved", "", "", "reserved"),
    KindDefinition("themes", "themes", "reserved", "", "", "reserved"),
    KindDefinition("monitors", "monitors", "reserved", "", "", "reserved"),
    KindDefinition("tools", "tools", "reserved", "", "", "reserved"),
)
KINDS_BY_ID: Dict[str, KindDefinition] = {kind.kind_id: kind for kind in KINDS}
KINDS_BY_DIRECTORY: Dict[str, KindDefinition] = {kind.directory: kind for kind in KINDS if kind.directory}
IMPLEMENTED_KINDS: Tuple[KindDefinition, ...] = tuple(kind for kind in KINDS if kind.is_implemented)

# ── What a set directory holds ─────────────────────────────────────────────────────────────────────────────────────
SET_CONF_FILE = "set.conf"
SET_ROOT_ENTRIES_ALLOWED: FrozenSet[str] = frozenset({
    "set.conf", "CHANGELOG.md", "README.md", "LICENSE", "LICENSES", "plugin.json", ".claude-plugin",
    "skills", "agents", "metadata",
})
# Reserved: named so a later format admits them without renaming; content under one is refused today.
SET_ROOT_ENTRIES_RESERVED: FrozenSet[str] = frozenset({"jobs", "libs", "variants"})
RELEASE_ROOT_ENTRIES_ALLOWED: FrozenSet[str] = frozenset({"SHA256SUMS", "SHA256SUMS.asc"})
CLAUDE_PLUGIN_DIRECTORY = ".claude-plugin"
CLAUDE_PLUGIN_MANIFEST = "plugin.json"
PORTABLE_PLUGIN_MANIFEST = "plugin.json"
PORTABLE_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
# A plugin manifest does not declare a component: each agent reads skills/ and agents/ from its default place,
# and a declared component is the shape a hook, a server or a command enters a set by.
PLUGIN_MANIFEST_COMPONENT_KEYS: FrozenSet[str] = frozenset({
    "skills", "agents", "commands", "hooks", "mcpServers", "lspServers", "outputStyles", "monitors", "workflows",
    "settings", "themes", "bin", "components", "extensions",
})

SKILL_ROOT_ENTRIES_ALLOWED: FrozenSet[str] = frozenset({
    "SKILL.md", "scripts", "references", "assets", "tests", "UPSTREAM.conf", "LICENSE", "LICENSES",
})
# OpenAI's skill sidecar carries invocation policy and tool dependencies; reserved until a tested profile admits it.
SKILL_SIDECAR_RESERVED_PATH = "agents/openai.yaml"

# Reserved file names are never discovered as assets, and are refused where they would be anything else.
RESERVED_FILE_NAMES: FrozenSet[str] = frozenset({
    "SHA256SUMS", "SHA256SUMS.asc", "UPSTREAM.conf", "asset.conf", "plugin.json", "README.md",
})
RESERVED_FILE_NAME_PATTERNS: Tuple[re.Pattern, ...] = (re.compile(r"^SHA512SUMS.*$"), re.compile(r"^.*\.oms\.sig$"))
RESERVED_DIRECTORY_NAMES: FrozenSet[str] = frozenset({".claude-plugin", ".agents", "metadata", "variants"})

# ── Frontmatter ────────────────────────────────────────────────────────────────────────────────────────────────────
DESCRIPTION_MAX_LENGTH = 1024
COMPATIBILITY_MAX_LENGTH = 500
METADATA_KEY_PREFIX = "ai-tools-"
SKILL_MD_LINES_WARN = 500

SKILL_FRONTMATTER_REQUIRED: Tuple[str, ...] = ("name", "description")
SKILL_FRONTMATTER_ALLOWED: FrozenSet[str] = frozenset({"name", "description", "license", "compatibility", "metadata"})
SKILL_FRONTMATTER_REFUSED_WHY: Dict[str, str] = {
    "allowed-tools": "pre-approves tools without a prompt while the skill runs",
}

SUBAGENT_FRONTMATTER_REQUIRED: Tuple[str, ...] = ("name", "description")
SUBAGENT_FRONTMATTER_ALLOWED: FrozenSet[str] = frozenset({
    "name", "description", "tools", "disallowedTools", "model", "effort", "maxTurns", "color", "skills", "metadata",
})
SUBAGENT_FRONTMATTER_REFUSED_WHY: Dict[str, str] = {
    "permissionMode": "changes the permissions a session runs under",
    "hooks": "runs commands on the subagent's events",
    "mcpServers": "adds tool servers",
    "memory": "refused until a set needs it and it is reviewed",
    "isolation": "refused until a set needs it and it is reviewed",
    "background": "refused until a set needs it and it is reviewed",
    "initialPrompt": "refused until a set needs it and it is reviewed",
    "omitClaudeMd": "refused until a set needs it and it is reviewed",
    "experimental": "refused until a set needs it and it is reviewed",
}

# ── Config keys ────────────────────────────────────────────────────────────────────────────────────────────────────
SET_CONF_REQUIRED: Tuple[str, ...] = ("format", "name", "version", "summary", "license", "maintainers", "source")
SET_CONF_OPTIONAL: Tuple[str, ...] = ("requires_base", "integrations", "requires_capabilities")
SET_CONF_LIST_KEYS: Tuple[str, ...] = ("maintainers", "integrations", "requires_capabilities")
PUBLISHER_CONF_REQUIRED: Tuple[str, ...] = ("publisher", "contact", "maintainers", "source", "description")
PUBLISHER_CONF_OPTIONAL: Tuple[str, ...] = ("licenses",)
UPSTREAM_CONF_REQUIRED: Tuple[str, ...] = ("source", "path", "revision", "license")
UPSTREAM_CONF_OPTIONAL: Tuple[str, ...] = ("signature", "signer")
UPSTREAM_CONF_FILE = "UPSTREAM.conf"
ASSET_CONF_FILE = "asset.conf"
ASSET_CONF_REQUIRED: Tuple[str, ...] = ("format",)
ASSET_CONF_OPTIONAL: Tuple[str, ...] = ("requires_capabilities", "requires_integrations", "targets")
ASSET_CONF_LIST_KEYS: Tuple[str, ...] = ("requires_capabilities", "requires_integrations", "targets")
METADATA_DIRECTORY = "metadata"
METADATA_ENTRIES_ALLOWED: FrozenSet[str] = frozenset({ASSET_CONF_FILE, UPSTREAM_CONF_FILE, "references"})
COMMIT_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

# The capabilities a format-1 reader implements; an unknown required one refuses the asset, or the set at set scope.
KNOWN_CAPABILITIES: FrozenSet[str] = frozenset({"skills.portable.v1", "subagents.claude.v1"})
INTEGRATION_TOKEN_PATTERN = re.compile(r"^integration-[a-z][a-z0-9-]*$")

# ── Files ──────────────────────────────────────────────────────────────────────────────────────────────────────────
FILE_MAX_BYTES = 1024 * 1024
SET_MAX_FILES = 2000
# A text file: UTF-8, no NUL, no C0 control other than tab, newline and carriage return, and none of the bidi controls
# that render text in another order than it is read.
BIDI_CONTROLS = "".join(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
CONTROL_CHARACTERS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f" + BIDI_CONTROLS + "]")
# Dynamic context injection: a line Claude Code runs as a shell command when the skill loads.
DYNAMIC_INJECTION_LINE = re.compile(r"^\s*!`")
DYNAMIC_INJECTION_FENCE = re.compile(r"^\s*(`{3,}|~{3,})!")
ABSOLUTE_PATH_REFUSED = re.compile(r"(?<![A-Za-z0-9_.-])/(?:opt/ai-tools|usr/share|usr/local/share)(?:/|\b)")
PROSE_FILE_SUFFIXES: FrozenSet[str] = frozenset({".md"})

# C# file-based apps: a directive the SDK reads from the top of a `.cs` script.
CS_DIRECTIVE = re.compile(r"^\s*#:(?P<directive>[a-z]+)\s+(?P<value>.*?)\s*$")
CS_SDKS_ALLOWED: FrozenSet[str] = frozenset({"Microsoft.NET.Sdk", "Microsoft.NET.Sdk.Web"})

# ── Licences ───────────────────────────────────────────────────────────────────────────────────────────────────────
# The GPLv3-compatible permissive default; `publisher.conf` `licenses=[...]` replaces it. CC-BY-4.0 is compatible
# and left off, for a publisher to opt in for reviewed prose assets.
DEFAULT_LICENSE_ALLOWLIST: Tuple[str, ...] = (
    "MIT", "MIT-0", "0BSD", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Apache-2.0", "CC0-1.0", "Unlicense",
)
LICENSE_TEXTS_DIRECTORY = "LICENSES"
# `reuse lint` reads the tag inside this pattern as a second expression of this file, so the line is ignored for it.
# REUSE-IgnoreStart
SPDX_HEADER = re.compile(r"SPDX-License-Identifier:\s*(?P<expression>[^\r\n*]+?)\s*(?:-->|\*/|\*\)|\s*$)")
# REUSE-IgnoreEnd
SPDX_HEADER_LINES_READ = 20

# ── Rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────
# Every finding carries one of these ids. A conformance fixture under fixtures/fail/<rule-id>/ fails on that rule alone,
# and FORMAT.md states each rule under its id; tests/test_rules.py holds the three to one another.
RULES: Dict[str, str] = {
    "repo.layout": "the repository root holds `publisher.conf` and `sets/`",
    "repo.publisher-conf": "`publisher.conf` reads as `KEY=value` with every required key",
    "repo.marketplace": "`.claude-plugin/marketplace.json` equals what `sync-manifests` writes from `set.conf` and `publisher.conf`",
    "set.conf.missing": "a set directory holds `set.conf`",
    "set.conf.syntax": "`set.conf` reads as `KEY=value`: every line is `KEY=value` or a comment, a key is written once, a list is `[a, b]`",
    "set.conf.required-key": "`set.conf` carries `format`, `name`, `version`, `summary`, `license`, `maintainers` and `source`",
    "set.conf.format": "`format` is the integer `1`",
    "set.conf.name": "`name` equals the set directory",
    "set.conf.version": "`version` is a semantic version",
    "set.conf.requires-capabilities": "every required capability is one the format defines",
    "set.conf.integrations": "every integration is written as `integration-<name>`",
    "set.conf.unknown-key": "an unknown key is reported; base reads past it",
    "set.entry.unknown": "a set directory holds `set.conf`, `CHANGELOG.md`, `README.md`, `LICENSE`, `LICENSES`, `plugin.json`, `.claude-plugin`, `skills`, `agents` and `metadata` alone",
    "set.entry.reserved": "`jobs`, `libs` and `variants` are reserved and do not hold any content",
    "set.manifest.plugin": "`plugin.json` and `.claude-plugin/plugin.json` carry the set's name, version, summary and licence",
    "set.manifest.components": "a plugin manifest does not declare a component key",
    "set.manifest.claude-plugin": "`.claude-plugin` holds `plugin.json` alone",
    "name.grammar": "a name is 1 to 64 characters of `a-z`, `0-9` and single hyphens, starting and ending with a letter or digit",
    "name.reserved-claude": "a name does not contain `anthropic` or `claude`",
    "name.reserved-word": "a set name does not start with a word from `reserved-words.txt`",
    "name.publisher": "a set is named `<publisher>` or `<publisher>-<topic>`; `core` belongs to dag-node",
    "name.asset-prefix": "an authored asset of `core` is named `ai-tools-<name>`; one of another set `<set>-<name>`",
    "name.frontmatter": "the frontmatter `name` equals the directory or file-stem name",
    "name.collision": "a skill and a subagent do not share a name",
    "name.composed-length": "`<plugin>:<name>` is at most 64 characters, the OpenAI submission limit",
    "kind.shape": "an entry under `skills/` is a directory holding `SKILL.md`, and one under `agents/` a `.md` file",
    "kind.reserved": "a reserved kind directory does not hold any content",
    "skill.entry.unknown": "a skill holds `SKILL.md`, `scripts`, `references`, `assets`, `tests`, `UPSTREAM.conf`, `LICENSE` and `LICENSES` alone",
    "skill.plugin-manifest": "a skill does not hold a `.claude-plugin` directory",
    "skill.sidecar": "`agents/openai.yaml` is reserved inside a skill",
    "skill.length": "`SKILL.md` is under 500 lines",
    "frontmatter.missing": "`SKILL.md` and a subagent file open with a frontmatter",
    "frontmatter.syntax": "the frontmatter is in the accepted YAML subset",
    "frontmatter.required": "`name` and `description` are present and non-empty",
    "frontmatter.refused-key": "a frontmatter key is on the kind's allowlist",
    "frontmatter.length": "`description` is at most 1024 characters and `compatibility` at most 500",
    "frontmatter.metadata": "`metadata` is a map of string values",
    "frontmatter.metadata-prefix": "a `metadata` key this format reads starts with `ai-tools-`",
    "body.dynamic-injection": "a line does not run a command when the asset loads",
    "body.absolute-path": "a body does not name an absolute path into `/opt/ai-tools`, `/usr/share` or `/usr/local/share`",
    "file.symlink": "a set does not hold a symbolic link",
    "file.hardlink": "a file has one link",
    "file.special": "every entry is a regular file or a directory",
    "file.binary": "every file is UTF-8 text without control or bidi characters",
    "file.size": "a file is at most 1 MiB and a set at most 2000 files",
    "file.reserved-name": "a reserved file name is used for its reserved purpose alone",
    "file.name": "a file name is printable and does not carry a newline",
    "cs.package": "a `.cs` script does not carry a `#:package` directive",
    "cs.sdk": "a `.cs` script's `#:sdk` is `Microsoft.NET.Sdk` or `Microsoft.NET.Sdk.Web`",
    "cs.project": "a `.cs` script's `#:project` names a `.csproj` inside the skill",
    "provenance.syntax": "`UPSTREAM.conf` reads as `KEY=value` with `source`, `path`, `revision` and `license`",
    "provenance.duplicate": "an asset has one `UPSTREAM.conf`, in the skill or under `metadata`",
    "metadata.kind": "`metadata/<kind>` is an implemented kind id",
    "metadata.asset": "`metadata/<kind>/<name>` names an asset the set holds",
    "metadata.entry": "`metadata/<kind>/<name>` holds `asset.conf`, `UPSTREAM.conf` and `references` alone",
    "metadata.asset-conf": "`asset.conf` reads as `KEY=value` with `format=1` and known capabilities, integration tokens and target names",
    "license.expression": "a licence is a well-formed SPDX expression without `WITH`, `LicenseRef` or a `+` suffix",
    "license.allowlist": "every identifier of a licence is on the list in force",
    "license.text": "`LICENSES/<identifier>.txt` exists for every identifier a set declares",
    "license.file": "every file of a repository states its licence in an SPDX header or a `REUSE.toml` annotation",
    "release.inventory": "`SHA256SUMS` lists every file of the built set and matches each",
}
RULES_WARNING: FrozenSet[str] = frozenset({
    "set.conf.unknown-key", "name.composed-length", "skill.length", "frontmatter.metadata-prefix",
})


def is_valid_name(name: str) -> bool:
    return bool(NAME_PATTERN.match(name))


def is_semver(version: str) -> bool:
    return bool(SEMVER_PATTERN.match(version))


def composed_plugin_name(set_name: str, asset_name: str) -> str:
    return f"{PLUGIN_NAME_PREFIX}{set_name}:{asset_name}"


def authored_asset_prefix(set_name: str) -> str:
    """The prefix an authored asset of `set_name` carries: ai-tools- for core and ai-tools, <set>- otherwise."""
    if set_name in (UPSTREAM_SET, BASE_SET):
        return UPSTREAM_ASSET_PREFIX
    return f"{set_name}-"


def is_reserved_file_name(name: str) -> bool:
    return name in RESERVED_FILE_NAMES or any(pattern.match(name) for pattern in RESERVED_FILE_NAME_PATTERNS)
