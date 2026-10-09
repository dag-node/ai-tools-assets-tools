# SPDX-License-Identifier: MIT
"""The format-1 validator over one set directory: every rule `asset_format.RULES` names, applied over one walk.

A set is read as data, once. The walk opens the set root and every directory under it by descriptor through
`safe_read`, so no symbolic link is followed and no path is reopened after it: every later rule reads the `FileRecord`
the walk kept, and the release inventory compares the digest the walk took. The walk is iterative and holds to the
budgets `asset_format` states; the first budget tripped is one `file.size` finding at the set root, after which the
walk stops and no later rule runs, so a tree too large to read whole is refused rather than judged in part. No file
under the set is imported, executed or sourced. The same function serves `tools/validate` over a publisher repository,
the conformance fixtures under `fixtures/`, and a built set under the release profile, so one implementation decides
what a set may carry.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

import asset_format as fmt
from findings import FindingCollector
from frontmatter import FrontmatterDocument, FrontmatterValue, Scalar, split_frontmatter
from key_value_config import KeyValueDocument, parse_key_value_text
from license_policy import check_declared_license, check_license_texts
from manifests import build_claude_plugin_document, build_portable_plugin_document, render_json
from markdown_links import iter_relative_link_targets
from portable_name import propose_portable_name
from safe_read import RefusedRead, open_directory, open_root, read_file

PROFILE_SOURCE = "source"
PROFILE_RELEASE = "release"
PROFILES: Tuple[str, ...] = (PROFILE_SOURCE, PROFILE_RELEASE)
TypedValue = Union[str, List[str], Dict[str, str]]
TEXT_SUFFIXES_KEPT: Tuple[str, ...] = (".md", ".conf", ".json", ".cs", ".txt", ".yaml", ".yml")
SHA256SUMS_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64}) [ *](?P<path>.+)$")


@dataclass
class FileRecord:
    """One regular file the walk read: its bytes and digest when it was read whole, and the text where the suffix is
    one the rules inspect. `data` and `digest` are None for a file over the size cap, whose first bytes alone were read."""

    relative_path: Path
    size: int
    data: Optional[bytes] = None
    digest: Optional[str] = None
    text: Optional[str] = None
    is_text: bool = True


@dataclass
class AssetRecord:
    kind_id: str
    name: str
    root: Path  # relative to the set root; the skill directory, or the subagent file
    is_vendored: bool = False


@dataclass
class SetSummary:
    """What the validator learned about a set, for the caller that renders or builds it."""

    set_name: str
    set_conf: Optional[KeyValueDocument] = None
    assets: List[AssetRecord] = field(default_factory=list)
    files: List[FileRecord] = field(default_factory=list)
    declared_licenses: Set[str] = field(default_factory=set)


@dataclass
class ValidationOptions:
    publisher: Optional[str]
    profile: str
    reserved_words: Set[str]
    license_allowlist: Sequence[str]
    license_text_directories: Sequence[Path]
    display_root: Path
    # The publisher.conf the manifests are rendered from; with one, each manifest equals that rendering whole.
    publisher_conf: Optional[KeyValueDocument] = None


def dynamic_injection_setting(publisher_conf: Optional[KeyValueDocument]) -> Tuple[bool, Optional[str]]:
    """Whether `publisher.conf` allows load-time substitution, and the reason its value is refused where it is.

    An absent file or key reads as not allowed; a value other than `yes` or `no` reads as not allowed too, with
    the reason for a `repo.publisher-conf` finding.
    """
    if publisher_conf is None or not publisher_conf.has(fmt.ALLOW_DYNAMIC_INJECTION_KEY):
        return False, None
    value = publisher_conf.get(fmt.ALLOW_DYNAMIC_INJECTION_KEY).strip()
    if value in ("yes", "no"):
        return value == "yes", None
    return False, f"`{fmt.ALLOW_DYNAMIC_INJECTION_KEY}={value}`; the value is `yes` or `no`"


def validate_set_directory(set_directory: Path, options: ValidationOptions, collector: FindingCollector,
                           parent_fd: Optional[int] = None) -> SetSummary:
    """Apply every set-level rule to `set_directory` and return what was read.

    The set root is opened as `set_directory.name` under `parent_fd` without following a symbolic link; with no
    `parent_fd`, the parent directory is the operator's argument and is opened by path.
    """
    validator = _SetValidator(set_directory, options, collector)
    return validator.run(parent_fd)


class _BudgetExceeded(Exception):
    """A walk budget tripped; the message names the budget and where."""


class _SetValidator:
    def __init__(self, set_directory: Path, options: ValidationOptions, collector: FindingCollector) -> None:
        self.set_directory = set_directory
        self.options = options
        self.collector = collector
        self.set_name = set_directory.name
        self.summary = SetSummary(set_name=self.set_name)
        self.files_by_path: Dict[Path, FileRecord] = {}
        self.directories: Set[Path] = set()
        self.bytes_read = 0
        self.dynamic_injection_allowed, _ = dynamic_injection_setting(options.publisher_conf)

    # ── reporting ──────────────────────────────────────────────────────────────────────────────────────────────
    def display(self, relative_path: Path) -> str:
        try:
            return str((self.set_directory / relative_path).relative_to(self.options.display_root))
        except ValueError:
            return str(self.set_directory / relative_path)

    def refuse(self, relative_path: Path, rule_id: str, message: str) -> None:
        self.collector.refuse(self.display(relative_path), rule_id, message)

    def warn(self, relative_path: Path, rule_id: str, message: str) -> None:
        self.collector.warn(self.display(relative_path), rule_id, message)

    # ── the run ────────────────────────────────────────────────────────────────────────────────────────────────
    def run(self, parent_fd: Optional[int]) -> SetSummary:
        root_fd = self.open_set_root(parent_fd)
        if root_fd is None:
            return self.summary
        try:
            complete = self.walk(root_fd)
        finally:
            os.close(root_fd)
        if not complete:
            return self.summary
        self.check_set_root_entries()
        set_conf = self.check_set_conf()
        self.check_claude_plugin_directory()
        if set_conf is not None:
            self.check_plugin_manifests(set_conf)
        self.check_set_name()
        skills = self.check_skills()
        subagents = self.check_subagents()
        self.check_name_collisions(skills, subagents)
        self.check_metadata_directory()
        self.check_reserved_kind_directories()
        if self.options.profile == PROFILE_RELEASE:
            self.check_release_inventory()
        return self.summary

    # ── the walk ───────────────────────────────────────────────────────────────────────────────────────────────
    def open_set_root(self, parent_fd: Optional[int]) -> Optional[int]:
        """The set root's descriptor, opened under `parent_fd` without following a link; None after a refusal."""
        own_parent: Optional[int] = None
        try:
            if parent_fd is None:
                parent_fd = own_parent = open_root(self.set_directory.parent)
            return open_directory(self.set_directory.name, parent_fd)
        except RefusedRead as refusal:
            self.refuse(Path("."), refusal.rule_id, refusal.message)
        except FileNotFoundError:
            self.refuse(Path("."), "set.conf.missing", "does not exist")
        except NotADirectoryError:
            self.refuse(Path("."), "set.conf.missing", "is not a directory")
        finally:
            if own_parent is not None:
                os.close(own_parent)
        return None

    def walk(self, root_fd: int) -> bool:
        """Read every entry under the set root once, by descriptor; False when a budget tripped and the walk stopped.

        An explicit stack of open directory descriptors replaces recursion, so depth is a budget and not a stack
        limit; a directory's entries are consumed as an iterator against the per-directory cap before they are sorted.
        """
        pending: List[Tuple[Path, int, int]] = [(Path("."), root_fd, 0)]
        try:
            while pending:
                relative_directory, dir_fd, depth = pending.pop()
                try:
                    subdirectories = self.read_directory(relative_directory, dir_fd, depth)
                finally:
                    if dir_fd != root_fd:
                        os.close(dir_fd)
                pending.extend(reversed(subdirectories))
        except _BudgetExceeded as budget:
            for _, dir_fd, _ in pending:
                if dir_fd != root_fd:
                    os.close(dir_fd)
            self.refuse(Path("."), "file.size", f"{budget}; the walk stopped there and no later rule was applied")
            return False
        return True

    def read_directory(self, relative_directory: Path, dir_fd: int, depth: int) -> List[Tuple[Path, int, int]]:
        """Record every entry of one directory and return its subdirectories, each opened, for the walk to continue."""
        entries = []
        try:
            with os.scandir(dir_fd) as listing:
                for entry in listing:
                    entries.append(entry)
                    if len(entries) > fmt.SET_MAX_DIRECTORY_ENTRIES:
                        raise _BudgetExceeded(f"`{relative_directory}` holds more than {fmt.SET_MAX_DIRECTORY_ENTRIES} entries")
        except OSError as error:
            self.refuse(relative_directory, "file.special", f"cannot be read: {error.strerror}")
            return []
        entries.sort(key=lambda entry: entry.name)
        subdirectories: List[Tuple[Path, int, int]] = []
        try:
            for entry in entries:
                relative_path = relative_directory / entry.name
                if not self.check_file_name(relative_path, entry.name):
                    continue
                if entry.is_symlink():
                    self.refuse(relative_path, "file.symlink", "is a symbolic link; a zip or a copy does not carry one the same way on every host")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    subdirectories.append((relative_path, self.open_subdirectory(relative_path, entry.name, dir_fd, depth + 1), depth + 1))
                    continue
                self.record_file(relative_path, entry.name, dir_fd)
        except _BudgetExceeded:
            for _, fd, _ in subdirectories:
                os.close(fd)
            raise
        return [(path, fd, child_depth) for path, fd, child_depth in subdirectories if fd >= 0]

    def open_subdirectory(self, relative_path: Path, name: str, dir_fd: int, depth: int) -> int:
        """Open a child directory under budget, recording it; -1 after a refusal, so the walk goes on past it."""
        if depth > fmt.SET_MAX_DEPTH:
            raise _BudgetExceeded(f"`{relative_path}` is {depth} levels deep; a set is at most {fmt.SET_MAX_DEPTH}")
        if len(self.directories) + 1 > fmt.SET_MAX_DIRECTORIES:
            raise _BudgetExceeded(f"the set holds more than {fmt.SET_MAX_DIRECTORIES} directories")
        try:
            fd = open_directory(name, dir_fd)
        except RefusedRead as refusal:
            self.refuse(relative_path, refusal.rule_id, refusal.message)
            return -1
        except OSError as error:
            self.refuse(relative_path, "file.special", f"cannot be read: {error.strerror}")
            return -1
        self.directories.add(relative_path)
        self.check_reserved_directory_name(relative_path, name)
        return fd

    def check_file_name(self, relative_path: Path, name: str) -> bool:
        """False after refusing a name outside the portable set; the walk does not read the entry, so no later rule
        reports on it. The path is shown with the name quoted, which keeps a newline in it on one line."""
        if fmt.is_portable_file_name(name):
            return True
        self.refuse(relative_path.parent / repr(name), "file.name",
                    f"is not a portable file name: A-Z, a-z, 0-9, `.`, `_` and `-`, not opening with `-`, at most "
                    f"{fmt.PORTABLE_FILE_NAME_MAX_BYTES} bytes; rename it, `{propose_portable_name(name)}` for example")
        return False

    def check_reserved_directory_name(self, relative_path: Path, name: str) -> None:
        if name == ".agents":
            self.refuse(relative_path, "file.reserved-name", "`.agents` is reserved and not used inside a set")
        elif name in (fmt.CLAUDE_PLUGIN_DIRECTORY, fmt.METADATA_DIRECTORY, "variants") and relative_path.parent != Path("."):
            if name == fmt.CLAUDE_PLUGIN_DIRECTORY and relative_path.parts[0] == "skills":
                self.refuse(relative_path, "skill.plugin-manifest", "a `.claude-plugin` directory inside a skill makes it a plugin of its own")
            else:
                self.refuse(relative_path, "file.reserved-name", f"`{name}` is reserved for the set root")

    def record_file(self, relative_path: Path, name: str, dir_fd: int) -> None:
        """Read one file under `dir_fd` through safe_read, keep its record, and count it against the budgets."""
        if len(self.files_by_path) + 1 > fmt.SET_MAX_FILES:
            raise _BudgetExceeded(f"the set holds more than {fmt.SET_MAX_FILES} files")
        try:
            read = read_file(name, dir_fd, fmt.FILE_MAX_BYTES)
        except RefusedRead as refusal:
            self.refuse(relative_path, refusal.rule_id, refusal.message)
            return
        except OSError as error:
            self.refuse(relative_path, "file.special", f"cannot be read: {error.strerror}")
            return
        record = FileRecord(relative_path=relative_path, size=read.size)
        # The bytes a truncated read did read count too, so a tree of many over-size files trips the aggregate budget.
        self.bytes_read += len(read.data)
        if self.bytes_read > fmt.SET_MAX_BYTES:
            raise _BudgetExceeded(f"the set holds more than {fmt.SET_MAX_BYTES} bytes")
        if read.truncated:
            self.refuse(relative_path, "file.size", f"is {read.size} bytes; a file is at most {fmt.FILE_MAX_BYTES}")
            record.is_text = False
        else:
            record.data = read.data
            record.digest = read.digest
            record.is_text = self.keep_text(relative_path, record)
        self.check_reserved_file_placement(relative_path)
        self.files_by_path[relative_path] = record
        self.summary.files.append(record)

    def keep_text(self, relative_path: Path, record: FileRecord) -> bool:
        """Decode the bytes read and keep the text where a rule inspects the suffix; False after a `file.binary` refusal."""
        try:
            text = record.data.decode("utf-8")
        except UnicodeDecodeError:
            self.refuse(relative_path, "file.binary", "is not UTF-8 text; a set carries text files alone")
            return False
        control = fmt.CONTROL_CHARACTERS.search(text)
        if control:
            self.refuse(relative_path, "file.binary", f"carries the control character U+{ord(control.group(0)):04X}")
            return False
        if relative_path.suffix in TEXT_SUFFIXES_KEPT or relative_path.name in ("SHA256SUMS", "LICENSE"):
            record.text = text
        return True

    def check_reserved_file_placement(self, relative_path: Path) -> None:
        name = relative_path.name
        parent = relative_path.parent
        parts = relative_path.parts
        if name in ("SHA256SUMS", "SHA256SUMS.asc") or name.startswith("SHA512SUMS"):
            if parent != Path(".") or self.options.profile != PROFILE_RELEASE:
                self.refuse(relative_path, "file.reserved-name", f"`{name}` is written by build-set at the set root of a release; it is not committed")
        elif name.endswith(".oms.sig"):
            self.refuse(relative_path, "file.reserved-name", "`*.oms.sig` is reserved for asset signing, which is not implemented")
        elif name == fmt.UPSTREAM_CONF_FILE:
            in_skill_root = len(parts) == 3 and parts[0] == "skills"
            in_metadata = len(parts) == 4 and parts[0] == fmt.METADATA_DIRECTORY
            if not (in_skill_root or in_metadata):
                self.refuse(relative_path, "file.reserved-name", "`UPSTREAM.conf` sits at a skill's root or under metadata/<kind>/<name>/")
        elif name == fmt.ASSET_CONF_FILE:
            if not (len(parts) == 4 and parts[0] == fmt.METADATA_DIRECTORY):
                self.refuse(relative_path, "file.reserved-name", "`asset.conf` sits under metadata/<kind>/<name>/")
        elif name == fmt.PORTABLE_PLUGIN_MANIFEST:
            # A `.claude-plugin/` inside a skill is reported by the walk as the skill's own plugin manifest.
            if parent != Path(".") and parent.name != fmt.CLAUDE_PLUGIN_DIRECTORY:
                self.refuse(relative_path, "file.reserved-name", "`plugin.json` is a set's manifest, at the set root and under .claude-plugin/")
        elif name == "README.md":
            if parent in (Path("skills"), Path("agents")) or parent in (Path(kind.directory) for kind in fmt.KINDS if kind.directory):
                self.refuse(relative_path, "file.reserved-name", "a README.md at a kind directory is read as an asset by an agent's scanner; the set's README.md sits at the set root")

    # ── the set root ───────────────────────────────────────────────────────────────────────────────────────────
    def root_entries(self) -> List[Path]:
        entries = {record.relative_path for record in self.files_by_path.values() if len(record.relative_path.parts) == 1}
        entries.update(directory for directory in self.directories if len(directory.parts) == 1)
        return sorted(entries)

    def entry_type(self, relative_path: Path) -> str:
        return fmt.ENTRY_DIRECTORY if relative_path in self.directories else fmt.ENTRY_FILE

    def check_entry_types(self, entries: Sequence[Path], types: Dict[str, str], rule_id: str) -> List[Path]:
        """Refuse each allowed entry that is a file where a directory is specified or the reverse; the rest are returned."""
        well_typed: List[Path] = []
        for entry in entries:
            expected = types.get(entry.name)
            if expected is not None and self.entry_type(entry) != expected:
                self.refuse(entry, rule_id, f"`{entry.name}` is a {self.entry_type(entry)}; the format specifies a {expected}")
            else:
                well_typed.append(entry)
        return well_typed

    def check_set_root_entries(self) -> None:
        allowed = set(fmt.SET_ROOT_ENTRIES_ALLOWED)
        if self.options.profile == PROFILE_RELEASE:
            allowed |= fmt.RELEASE_ROOT_ENTRIES_ALLOWED
        entries = self.root_entries()
        kind_roots = [entry for entry in entries if entry.name in fmt.KINDS_BY_DIRECTORY and fmt.KINDS_BY_DIRECTORY[entry.name].is_implemented]
        other = [entry for entry in entries if entry not in kind_roots]
        self.check_entry_types(kind_roots, fmt.SET_ROOT_ENTRY_TYPES, "kind.shape")
        for entry in self.check_entry_types(other, fmt.SET_ROOT_ENTRY_TYPES, "set.entry.unknown"):
            name = entry.name
            if name in allowed:
                continue
            if name in fmt.RELEASE_ROOT_ENTRIES_ALLOWED or name.startswith("SHA512SUMS"):
                continue  # reported as file.reserved-name under the source profile
            if name in fmt.SET_ROOT_ENTRIES_RESERVED:
                shown = f"{name}/" if entry in self.directories else name
                self.refuse(entry, "set.entry.reserved", f"`{shown}` is reserved; no capability of format 1 allows content at it")
                continue
            kind = fmt.KINDS_BY_DIRECTORY.get(name)
            if kind is not None and kind.support == "reserved" and entry in self.directories:
                continue  # reported as kind.reserved
            self.refuse(entry, "set.entry.unknown", "a set directory holds " + ", ".join(sorted(fmt.SET_ROOT_ENTRIES_ALLOWED)) + " alone")

    def check_set_conf(self) -> Optional[KeyValueDocument]:
        relative_path = Path(fmt.SET_CONF_FILE)
        record = self.files_by_path.get(relative_path)
        if record is None:
            self.refuse(relative_path, "set.conf.missing", "a set directory holds set.conf")
            return None
        if record.text is None:
            return None
        set_conf = parse_key_value_text(record.text)
        for message in set_conf.syntax_errors():
            self.refuse(relative_path, "set.conf.syntax", message)
        # An empty list key (`maintainers=`) is the list grammar's finding in the loop over SET_CONF_LIST_KEYS, so the
        # two rules do not both fire.
        missing = [key for key in fmt.SET_CONF_REQUIRED
                   if not set_conf.has(key) or (key not in fmt.SET_CONF_LIST_KEYS and not set_conf.get(key).strip())]
        if missing:
            self.refuse(relative_path, "set.conf.required-key", "missing or empty: " + ", ".join(f"`{key}=`" for key in missing))
        if set_conf.has("format") and set_conf.get("format").strip() != str(fmt.FORMAT_VERSION):
            self.refuse(relative_path, "set.conf.format", f"`format={set_conf.get('format')}`; this format is `{fmt.FORMAT_VERSION}`")
        if set_conf.has("name") and set_conf.get("name") != self.set_name:
            self.refuse(relative_path, "set.conf.name", f"`name={set_conf.get('name')}` differs from the set's directory `{self.set_name}`")
        if set_conf.has("version") and not fmt.is_semver(set_conf.get("version")):
            self.refuse(relative_path, "set.conf.version", f"`version={set_conf.get('version')}` is not a semantic version")
        for key in fmt.SET_CONF_LIST_KEYS:
            if set_conf.has(key):
                items, reason = set_conf.get_list(key)
                if reason is not None:
                    self.refuse(relative_path, "set.conf.syntax", f"`{key}` is not a list ({reason}); write it as [a, b]")
                elif not items and key in fmt.SET_CONF_REQUIRED:
                    self.refuse(relative_path, "set.conf.required-key", f"`{key}=[]` is empty; a set declares at least one")
                elif key == "requires_capabilities":
                    for capability in items:
                        if capability not in fmt.KNOWN_CAPABILITIES:
                            self.refuse(relative_path, "set.conf.requires-capabilities", f"`{capability}` is not a capability this format defines; the set is refused as a whole")
                elif key == "requires_integrations":
                    for integration in items:
                        if not fmt.INTEGRATION_TOKEN_PATTERN.match(integration):
                            self.refuse(relative_path, "set.conf.requires-integrations", f"`{integration}` is not written as integration-<name>")
        self.check_known_keys(relative_path, set_conf, set(fmt.SET_CONF_REQUIRED) | set(fmt.SET_CONF_OPTIONAL))
        if set_conf.has("license"):
            evaluation = check_declared_license(self.collector, self.display(relative_path), set_conf.get("license"),
                                                self.options.license_allowlist, "the set's licence")
            if evaluation.is_allowed:
                self.summary.declared_licenses.update(evaluation.identifiers)
                check_license_texts(self.collector, self.display(relative_path), evaluation.identifiers,
                                    self.carried_license_texts(), self.options.license_text_directories)
        self.summary.set_conf = set_conf
        return set_conf

    def check_known_keys(self, relative_path: Path, document: KeyValueDocument, known: Set[str]) -> None:
        """Refuse a key outside the file's table unless it is an `x_<key>` extension key, which is read past."""
        for key in document.values:
            if key not in known and not fmt.EXTENSION_KEY_PATTERN.match(key):
                self.refuse(relative_path, "set.conf.unknown-key", f"`{key}` is not a key this format reads; a publisher's own key is `x_<key>`")

    def carried_license_texts(self, *scopes: Path) -> Set[str]:
        """The identifiers whose `LICENSES/<identifier>.txt` the walk read at the set root or under one of `scopes`."""
        directories = {Path(fmt.LICENSE_TEXTS_DIRECTORY), *(scope / fmt.LICENSE_TEXTS_DIRECTORY for scope in scopes)}
        return {path.stem for path in self.files_by_path if path.parent in directories and path.suffix == ".txt"}

    def check_claude_plugin_directory(self) -> None:
        directory = Path(fmt.CLAUDE_PLUGIN_DIRECTORY)
        if directory not in self.directories:
            return
        for record in self.files_by_path.values():
            if record.relative_path.parent == directory and record.relative_path.name != fmt.CLAUDE_PLUGIN_MANIFEST:
                self.refuse(record.relative_path, "set.manifest.claude-plugin", "`.claude-plugin/` holds plugin.json alone")
        for sub in self.directories:
            if sub.parent == directory:
                self.refuse(sub, "set.manifest.claude-plugin", "`.claude-plugin/` holds plugin.json alone")

    def check_plugin_manifests(self, set_conf: KeyValueDocument) -> None:
        wanted_name = fmt.PLUGIN_NAME_PREFIX + self.set_name
        for relative_path in (Path(fmt.PORTABLE_PLUGIN_MANIFEST), Path(fmt.CLAUDE_PLUGIN_DIRECTORY) / fmt.CLAUDE_PLUGIN_MANIFEST):
            record = self.files_by_path.get(relative_path)
            if record is None:
                self.refuse(relative_path, "set.manifest.plugin", "is absent; sync-manifests writes it from set.conf")
                continue
            if record.text is None:
                continue
            if record.size > fmt.PLUGIN_MANIFEST_MAX_BYTES:
                self.refuse(relative_path, "set.manifest.plugin", f"is {record.size} bytes; a manifest is at most {fmt.PLUGIN_MANIFEST_MAX_BYTES}")
                continue
            try:
                document = json.loads(record.text)
            except (ValueError, RecursionError) as error:
                self.refuse(relative_path, "set.manifest.plugin", f"cannot be read as JSON: {error if isinstance(error, ValueError) else 'nests too deep'}")
                continue
            if not isinstance(document, dict):
                self.refuse(relative_path, "set.manifest.plugin", "is not a JSON object")
                continue
            expected = {"name": wanted_name, "version": set_conf.get("version"), "description": set_conf.get("summary"),
                        "license": set_conf.get("license")}
            for key, value in expected.items():
                if document.get(key) != value:
                    self.refuse(relative_path, "set.manifest.plugin", f"`{key}` is `{document.get(key)}`, expected `{value}` from set.conf")
            is_portable = relative_path.name == fmt.PORTABLE_PLUGIN_MANIFEST and relative_path.parent == Path(".")
            if is_portable and document.get("$schema") != fmt.PORTABLE_PLUGIN_SCHEMA:
                self.refuse(relative_path, "set.manifest.plugin", f"`$schema` is `{document.get('$schema')}`, expected `{fmt.PORTABLE_PLUGIN_SCHEMA}`")
            declared = sorted(key for key in document if key in fmt.PLUGIN_MANIFEST_COMPONENT_KEYS)
            if declared:
                self.refuse(relative_path, "set.manifest.components", "declares " + ", ".join(f"`{key}`" for key in declared) + "; a set manifest declares no component, each agent reads skills/ and agents/ from its default place")
            unknown = sorted(key for key in document if key not in fmt.PLUGIN_MANIFEST_ALLOWED_KEYS and key not in fmt.PLUGIN_MANIFEST_COMPONENT_KEYS)
            if unknown:
                self.refuse(relative_path, "set.manifest.plugin", "carries " + ", ".join(f"`{key}`" for key in unknown) + "; a set manifest carries " + ", ".join(sorted(fmt.PLUGIN_MANIFEST_ALLOWED_KEYS)) + " alone")
            if self.options.publisher_conf is not None and not declared and not unknown:
                rendered = render_json((build_portable_plugin_document if is_portable else build_claude_plugin_document)(set_conf, self.options.publisher_conf))
                if record.text != rendered:
                    self.refuse(relative_path, "set.manifest.plugin", "differs from what sync-manifests writes from set.conf and publisher.conf")

    def check_set_name(self) -> None:
        relative_path = Path(fmt.SET_CONF_FILE)
        if not self.check_name_grammar(relative_path, "set name", self.set_name):
            return
        first_word = self.set_name.split("-", 1)[0]
        if first_word in self.options.reserved_words:
            self.refuse(relative_path, "name.reserved-word", f"set `{self.set_name}` starts with `{first_word}`, a word in reserved-words.txt")
        publisher = self.options.publisher
        if publisher is None:
            return
        if self.set_name == fmt.UPSTREAM_SET:
            if publisher != fmt.UPSTREAM_PUBLISHER:
                self.refuse(relative_path, "name.publisher", f"`core` belongs to {fmt.UPSTREAM_PUBLISHER}; a set published by `{publisher}` is named `{publisher}` or `{publisher}-<topic>`")
        elif self.set_name != publisher and not self.set_name.startswith(publisher + "-"):
            self.refuse(relative_path, "name.publisher", f"set `{self.set_name}` does not start with its publisher: name it `{publisher}` or `{publisher}-<topic>`")

    def check_name_grammar(self, relative_path: Path, what: str, name: str) -> bool:
        if not fmt.is_valid_name(name):
            self.refuse(relative_path, "name.grammar", f"{what} `{name}` is not 1-64 characters of a-z, 0-9 and single hyphens, starting and ending with a letter or digit")
            return False
        for word in fmt.CLAUDE_RESERVED_WORDS:
            if word in name:
                self.refuse(relative_path, "name.reserved-claude", f"{what} `{name}` contains `{word}`, which Claude reserves")
        return True

    # ── skills ─────────────────────────────────────────────────────────────────────────────────────────────────
    def children_of(self, relative_directory: Path) -> Tuple[List[Path], List[Path]]:
        files = sorted(path for path in self.files_by_path if path.parent == relative_directory)
        directories = sorted(path for path in self.directories if path.parent == relative_directory)
        return files, directories

    def check_skills(self) -> Dict[str, AssetRecord]:
        skills: Dict[str, AssetRecord] = {}
        kind_directory = Path("skills")
        if kind_directory not in self.directories:
            return skills
        files, directories = self.children_of(kind_directory)
        for stray in files:
            if stray.name != "README.md":
                self.refuse(stray, "kind.shape", "an entry under skills/ is a directory holding SKILL.md")
        for skill_root in directories:
            name = skill_root.name
            entry_file = skill_root / "SKILL.md"
            if entry_file not in self.files_by_path:
                what = "is a directory" if entry_file in self.directories else "is absent"
                self.refuse(skill_root, "kind.shape", f"a skill directory holds a SKILL.md file; `{name}/SKILL.md` {what}")
                continue
            if not self.check_name_grammar(skill_root, "skill", name):
                continue
            is_vendored = (skill_root / fmt.UPSTREAM_CONF_FILE) in self.files_by_path or self.has_metadata_upstream("skills", name)
            record = AssetRecord("skills", name, skill_root, is_vendored)
            skills[name] = record
            self.summary.assets.append(record)
            self.check_asset_prefix(skill_root, "skill", name, is_vendored)
            self.check_composed_length(skill_root, name)
            self.check_skill_entries(skill_root)
            self.check_skill_frontmatter_and_body(skill_root, name)
            self.check_skill_scripts(skill_root)
            if (skill_root / fmt.UPSTREAM_CONF_FILE) in self.files_by_path:
                self.check_upstream_conf(skill_root / fmt.UPSTREAM_CONF_FILE)
                if self.has_metadata_upstream("skills", name):
                    self.refuse(skill_root / fmt.UPSTREAM_CONF_FILE, "provenance.duplicate", f"skill `{name}` also has metadata/skills/{name}/UPSTREAM.conf; an asset has one provenance declaration")
        return skills

    def has_metadata_upstream(self, kind_id: str, name: str) -> bool:
        return (Path(fmt.METADATA_DIRECTORY) / kind_id / name / fmt.UPSTREAM_CONF_FILE) in self.files_by_path

    def check_asset_prefix(self, relative_path: Path, what: str, name: str, is_vendored: bool) -> None:
        if is_vendored:
            return
        wanted = fmt.authored_asset_prefix(self.set_name)
        if self.set_name not in (fmt.UPSTREAM_SET, fmt.BASE_SET) and name.startswith(fmt.UPSTREAM_ASSET_PREFIX):
            self.refuse(relative_path, "name.asset-prefix", f"{what} `{name}` carries `{fmt.UPSTREAM_ASSET_PREFIX}`, which belongs to `core`; an authored asset of `{self.set_name}` is named `{wanted}<name>`")
        elif not name.startswith(wanted):
            self.refuse(relative_path, "name.asset-prefix", f"{what} `{name}` does not carry the set's prefix `{wanted}`; a vendored asset records its origin in UPSTREAM.conf")

    def check_composed_length(self, relative_path: Path, name: str) -> None:
        composed = fmt.composed_plugin_name(self.set_name, name)
        if len(composed) > fmt.COMPOSED_NAME_MAX_LENGTH:
            self.warn(relative_path, "name.composed-length", f"`{composed}` is {len(composed)} characters; OpenAI's plugin submission takes at most {fmt.COMPOSED_NAME_MAX_LENGTH}")

    def check_skill_entries(self, skill_root: Path) -> None:
        files, directories = self.children_of(skill_root)
        sidecar = skill_root / fmt.SKILL_SIDECAR_RESERVED_PATH
        has_sidecar = sidecar in self.files_by_path
        for entry in self.check_entry_types(files + directories, fmt.SKILL_ROOT_ENTRY_TYPES, "skill.entry.unknown"):
            if entry.name not in fmt.SKILL_ROOT_ENTRIES_ALLOWED:
                if entry.name == fmt.CLAUDE_PLUGIN_DIRECTORY or (has_sidecar and entry == sidecar.parent):
                    continue  # reported by the walk, or as the sidecar
                self.refuse(entry, "skill.entry.unknown", "a skill holds " + ", ".join(sorted(fmt.SKILL_ROOT_ENTRIES_ALLOWED)) + " alone")
        if has_sidecar:
            self.refuse(sidecar, "skill.sidecar", "`agents/openai.yaml` carries invocation policy and tool dependencies; it is reserved until a tested profile allows it")

    def check_skill_frontmatter_and_body(self, skill_root: Path, name: str) -> None:
        entry_file = skill_root / "SKILL.md"
        record = self.files_by_path[entry_file]
        if record.text is None:
            return
        line_count = record.text.count("\n") + (0 if record.text.endswith("\n") else 1)
        if line_count > fmt.SKILL_MD_LINES_WARN:
            self.warn(entry_file, "skill.length", f"is {line_count} lines; the specification's guidance is under {fmt.SKILL_MD_LINES_WARN}, with longer material in files it links to")
        document, _ = split_frontmatter(record.text)
        typed = self.check_frontmatter(entry_file, document, name, fmt.SKILL_FRONTMATTER_REQUIRED, fmt.SKILL_FRONTMATTER_TYPES,
                                       fmt.SKILL_FRONTMATTER_REFUSED_WHY)
        compatibility = typed.get("compatibility")
        if isinstance(compatibility, str) and len(compatibility) > fmt.COMPATIBILITY_MAX_LENGTH:
            self.refuse(entry_file, "frontmatter.length", f"`compatibility` is {len(compatibility)} characters; at most {fmt.COMPATIBILITY_MAX_LENGTH}")
        license_field = typed.get("license")
        if isinstance(license_field, str) and license_field:
            evaluation = check_declared_license(self.collector, self.display(entry_file), license_field,
                                                self.options.license_allowlist, "the skill's licence")
            if evaluation.is_allowed:
                self.summary.declared_licenses.update(evaluation.identifiers)
                check_license_texts(self.collector, self.display(entry_file), evaluation.identifiers,
                                    self.carried_license_texts(skill_root), self.options.license_text_directories)
        self.check_body(entry_file, record.text, self.dynamic_injection_refusal("skills", name))
        self.check_relative_links(entry_file, record.text, {path for path in self.files_by_path if skill_root in path.parents})
        for relative_path, other in self.files_by_path.items():
            if relative_path != entry_file and relative_path.suffix in fmt.PROSE_FILE_SUFFIXES and other.text is not None \
                    and skill_root in relative_path.parents:
                self.check_body(relative_path, other.text, None)

    def check_frontmatter(self, relative_path: Path, document: FrontmatterDocument, expected_name: str,
                          required: Sequence[str], types: Dict[str, str], refused_why: Dict[str, str]) -> Dict[str, TypedValue]:
        """Apply the kind's schema and return the typed fields; a field that failed its type is left out."""
        typed: Dict[str, TypedValue] = {}
        if not document.present:
            self.refuse(relative_path, "frontmatter.missing", "opens with a `---` frontmatter carrying name and description")
            return typed
        for error in document.errors:
            self.refuse(relative_path, "frontmatter.syntax", error)
        if document.has_errors():
            return typed
        for key, value in document.values.items():
            if key not in types:
                why = refused_why.get(key, "is not on the allowlist for this kind; propose it in an issue with the asset that needs it")
                self.refuse(relative_path, "frontmatter.refused-key", f"`{key}` {why}")
                continue
            if key in required and isinstance(value, Scalar) and value.is_omitted:
                self.refuse(relative_path, "frontmatter.required", f"`{key}` is missing or empty")
                continue
            result = self.typed_field(relative_path, key, value, types[key])
            if result is not None:
                typed[key] = result
        for key in required:
            value = typed.get(key)
            if key in document.values and key not in typed:
                continue  # its type was refused above
            if not isinstance(value, str) or not value.strip():
                self.refuse(relative_path, "frontmatter.required", f"`{key}` is missing or empty")
        name_value = typed.get("name")
        if isinstance(name_value, str) and name_value and name_value != expected_name:
            self.refuse(relative_path, "name.frontmatter", f"frontmatter `name: {name_value}` differs from `{expected_name}`")
        description = typed.get("description")
        if isinstance(description, str) and len(description) > fmt.DESCRIPTION_MAX_LENGTH:
            self.refuse(relative_path, "frontmatter.length", f"`description` is {len(description)} characters; at most {fmt.DESCRIPTION_MAX_LENGTH}")
        metadata = typed.get("metadata")
        if isinstance(metadata, dict):
            for key in metadata:
                if not key.startswith(fmt.METADATA_KEY_PREFIX):
                    self.warn(relative_path, "frontmatter.metadata-prefix", f"`metadata.{key}`: a key this format reads starts with `{fmt.METADATA_KEY_PREFIX}`; another is left to its reader")
        return typed

    def typed_field(self, relative_path: Path, key: str, value: FrontmatterValue, field_type: str) -> Optional[TypedValue]:
        """The field's value under its declared type, or None after refusing it under `frontmatter.type` (the shape
        of `metadata` under `frontmatter.metadata`, the rule that names it)."""
        if field_type == fmt.FIELD_STRING_MAP:
            if not isinstance(value, dict):
                self.refuse(relative_path, "frontmatter.metadata", f"`{key}` is a map of string values")
                return None
            for map_key, scalar in value.items():
                if scalar.is_omitted:
                    self.refuse(relative_path, "frontmatter.type", f"`{key}.{map_key}:` has no value, which YAML reads as null; quote an empty string, or leave the key out")
                    return None
                if scalar.is_yaml_typed:
                    self.refuse(relative_path, "frontmatter.type", f"`{key}.{map_key}: {scalar.text}` reads as a number, a boolean, null or a date in YAML; quote it")
                    return None
            return {map_key: scalar.text for map_key, scalar in value.items()}
        if field_type == fmt.FIELD_STRING_LIST:
            if isinstance(value, dict):
                self.refuse(relative_path, "frontmatter.type", f"`{key}` is a list of strings, not a map")
                return None
            items = value if isinstance(value, list) else [Scalar(item.strip(), value.quoted) for item in value.text.split(",")] if value.text.strip() else []
            for item in items:
                if not item.text:
                    self.refuse(relative_path, "frontmatter.type", f"`{key}` carries an empty item")
                    return None
                if item.is_yaml_typed:
                    self.refuse(relative_path, "frontmatter.type", f"`{key}` item `{item.text}` reads as a number, a boolean or null in YAML")
                    return None
            return [item.text for item in items]
        if not isinstance(value, Scalar):
            self.refuse(relative_path, "frontmatter.type", f"`{key}` is a {field_type}, not a " + ("list" if isinstance(value, list) else "map"))
            return None
        if field_type == fmt.FIELD_INTEGER:
            if value.quoted or not fmt.INTEGER_PATTERN.match(value.text):
                self.refuse(relative_path, "frontmatter.type", f"`{key}: {value.text}` is not an unquoted positive integer")
                return None
            return value.text
        if value.is_omitted:
            self.refuse(relative_path, "frontmatter.type", f"`{key}:` has no value, which YAML reads as null; quote an empty string, or omit the key")
            return None
        if value.is_yaml_typed:
            self.refuse(relative_path, "frontmatter.type", f"`{key}: {value.text}` reads as a number, a boolean, null or a date in YAML; quote it")
            return None
        return value.text

    def dynamic_injection_refusal(self, kind_id: str, name: str) -> Optional[str]:
        """Why a substitution in the asset's entry file is refused, or None where the asset declares the capability
        and publisher.conf allows it. A declaration publisher.conf does not allow is refused here, used or not."""
        asset_conf = Path(fmt.METADATA_DIRECTORY) / kind_id / name / fmt.ASSET_CONF_FILE
        if fmt.DYNAMIC_INJECTION_CAPABILITY not in self.declared_capabilities(asset_conf):
            return f"the asset does not declare `{fmt.DYNAMIC_INJECTION_CAPABILITY}` in {asset_conf}"
        if self.dynamic_injection_allowed:
            return None
        self.refuse(asset_conf, "body.dynamic-injection", f"declares `{fmt.DYNAMIC_INJECTION_CAPABILITY}`, which publisher.conf does not allow; a publisher allows it with `{fmt.ALLOW_DYNAMIC_INJECTION_KEY}=yes`")
        return f"publisher.conf does not allow `{fmt.DYNAMIC_INJECTION_CAPABILITY}`"

    def declared_capabilities(self, asset_conf: Path) -> Set[str]:
        """The capabilities `asset_conf` requires; empty where the file is absent, unread or malformed, which
        check_asset_conf reports, so a declaration the validator cannot read does not allow anything."""
        record = self.files_by_path.get(asset_conf)
        if record is None or record.text is None:
            return set()
        document = parse_key_value_text(record.text)
        if document.syntax_errors():
            return set()
        items, reason = document.get_list("requires_capabilities")
        return set(items) if reason is None else set()

    def check_body(self, relative_path: Path, text: str, injection_refusal: Optional[str]) -> None:
        """Scan every line of the file -- an entry file's frontmatter included, since a loader expands a substitution
        wherever it stands -- for a dynamic substitution (where `injection_refusal` names why one is refused) and
        a refused absolute path."""
        for line_number, line in enumerate(text.split("\n"), start=1):
            if injection_refusal and (fmt.DYNAMIC_INJECTION_INLINE.search(line) or fmt.DYNAMIC_INJECTION_FENCE.match(line)):
                self.refuse(relative_path, "body.dynamic-injection", f"line {line_number} runs a command when the asset loads, before a person or the model reads it; {injection_refusal}")
            match = fmt.ABSOLUTE_PATH_REFUSED.search(line)
            if match:
                self.refuse(relative_path, "body.absolute-path", f"line {line_number} names `{match.group(0).strip()}`; a skill names its own files relative to its root, and another skill by name")

    def check_relative_links(self, entry_file: Path, text: str, asset_files: Set[Path]) -> None:
        """Refuse each relative link in the entry file's body whose target, resolved lexically against the entry file's
        directory, is not one of `asset_files`, the asset's regular files; a subagent's asset is its one file."""
        document, body = split_frontmatter(text)
        first_line_number = document.body_start_line if document.present and document.body_start_line else 1
        for line_number, target in iter_relative_link_targets(body.split("\n"), first_line_number):
            resolved = Path(posixpath.normpath(posixpath.join(entry_file.parent.as_posix(), target)))
            if resolved not in asset_files:
                self.refuse(entry_file, "body.relative-link", f"line {line_number} links `{target}`, which is not a file of this asset; a skill links its own files relative to its root, and another skill by name")

    def check_skill_scripts(self, skill_root: Path) -> None:
        for relative_path, record in self.files_by_path.items():
            if relative_path.suffix != ".cs" or skill_root not in relative_path.parents or record.text is None:
                continue
            for line_number, line in enumerate(record.text.split("\n"), start=1):
                match = fmt.CS_DIRECTIVE.match(line)
                if not match:
                    if line.strip() and not line.lstrip().startswith(("#", "//")):
                        break  # directives sit at the top of a file-based app
                    continue
                directive, value = match.group("directive"), match.group("value")
                if directive == "package":
                    self.refuse(relative_path, "cs.package", f"line {line_number}: `#:package {value}` fetches code at run time that SHA256SUMS and the signature do not cover")
                elif directive == "sdk":
                    sdk = value.split("@", 1)[0].strip()
                    if sdk not in fmt.CS_SDKS_ALLOWED:
                        self.refuse(relative_path, "cs.sdk", f"line {line_number}: `#:sdk {value}`; " + " or ".join(sorted(fmt.CS_SDKS_ALLOWED)) + " ship with the SDK, another is fetched from NuGet")
                elif directive == "project":
                    self.check_cs_project(relative_path, skill_root, line_number, value.strip())

    def check_cs_project(self, script: Path, skill_root: Path, line_number: int, value: str) -> None:
        """`#:project` resolved lexically against the script's directory: inside the skill, a `.csproj`, and shipped."""
        if Path(value).is_absolute():
            self.refuse(script, "cs.project", f"line {line_number}: `#:project {value}` is absolute; a project is named relative to the script")
            return
        target = Path(posixpath.normpath(posixpath.join(script.parent.as_posix(), value)))
        if target.parts[:1] == ("..",) or (skill_root != target.parent and skill_root not in target.parents):
            self.refuse(script, "cs.project", f"line {line_number}: `#:project {value}` names a project outside the skill")
        elif target.suffix != ".csproj":
            self.refuse(script, "cs.project", f"line {line_number}: `#:project {value}` does not name a `.csproj`")
        elif target not in self.files_by_path:
            self.refuse(script, "cs.project", f"line {line_number}: `#:project {value}` names `{target}`, which the skill does not ship")

    # ── subagents ──────────────────────────────────────────────────────────────────────────────────────────────
    def check_subagents(self) -> Dict[str, AssetRecord]:
        subagents: Dict[str, AssetRecord] = {}
        kind_directory = Path("agents")
        if kind_directory not in self.directories:
            return subagents
        files, directories = self.children_of(kind_directory)
        for stray in directories:
            self.refuse(stray, "kind.shape", "an entry under agents/ is a <name>.md file; Claude Code reads every .md in agents/ as a subagent")
        for file_path in files:
            if file_path.suffix != ".md" or file_path.name == "README.md":
                if file_path.name != "README.md":
                    self.refuse(file_path, "kind.shape", "an entry under agents/ is a <name>.md file")
                continue
            name = file_path.stem
            if not self.check_name_grammar(file_path, "subagent", name):
                continue
            is_vendored = self.has_metadata_upstream("subagents", name)
            record = AssetRecord("subagents", name, file_path, is_vendored)
            subagents[name] = record
            self.summary.assets.append(record)
            self.check_asset_prefix(file_path, "subagent", name, is_vendored)
            self.check_composed_length(file_path, name)
            text = self.files_by_path[file_path].text
            if text is None:
                continue
            document, _ = split_frontmatter(text)
            self.check_frontmatter(file_path, document, name, fmt.SUBAGENT_FRONTMATTER_REQUIRED,
                                   fmt.SUBAGENT_FRONTMATTER_TYPES, fmt.SUBAGENT_FRONTMATTER_REFUSED_WHY)
            self.check_body(file_path, text, self.dynamic_injection_refusal("subagents", name))
            self.check_relative_links(file_path, text, {file_path})
        return subagents

    def check_name_collisions(self, skills: Dict[str, AssetRecord], subagents: Dict[str, AssetRecord]) -> None:
        for name in sorted(set(skills) & set(subagents)):
            self.refuse(subagents[name].root, "name.collision", f"`{name}` is both a skill and a subagent; an agent lists the two kinds in one list")

    # ── metadata ───────────────────────────────────────────────────────────────────────────────────────────────
    def check_metadata_directory(self) -> None:
        metadata_root = Path(fmt.METADATA_DIRECTORY)
        if metadata_root not in self.directories:
            return
        asset_names = {(asset.kind_id, asset.name) for asset in self.summary.assets}
        files, kind_directories = self.children_of(metadata_root)
        for stray in files:
            self.refuse(stray, "metadata.entry", "metadata/ holds <kind>/<name>/ directories alone")
        for kind_directory in kind_directories:
            kind = fmt.KINDS_BY_ID.get(kind_directory.name)
            if kind is None or not kind.is_implemented:
                self.refuse(kind_directory, "metadata.kind", f"`{kind_directory.name}` is not an implemented kind id (" + ", ".join(k.kind_id for k in fmt.IMPLEMENTED_KINDS) + ")")
                continue
            kind_files, asset_directories = self.children_of(kind_directory)
            for stray in kind_files:
                self.refuse(stray, "metadata.entry", "metadata/<kind>/ holds <name>/ directories alone")
            for asset_directory in asset_directories:
                if (kind.kind_id, asset_directory.name) not in asset_names:
                    self.refuse(asset_directory, "metadata.asset", f"the set holds no {kind.kind_id[:-1]} named `{asset_directory.name}`")
                asset_files, asset_subdirectories = self.children_of(asset_directory)
                for entry in self.check_entry_types(asset_files + asset_subdirectories, fmt.METADATA_ENTRY_TYPES, "metadata.entry"):
                    if entry.name not in fmt.METADATA_ENTRIES_ALLOWED:
                        self.refuse(entry, "metadata.entry", "metadata/<kind>/<name>/ holds asset.conf, UPSTREAM.conf and references/ alone")
                # A reference under metadata is an asset's `.md` file like one under a skill, so the same body rule
                # reads it.
                references = asset_directory / "references"
                for relative_path, other in self.files_by_path.items():
                    if references in relative_path.parents and relative_path.suffix in fmt.PROSE_FILE_SUFFIXES and other.text is not None:
                        self.check_body(relative_path, other.text, None)
                if (asset_directory / fmt.ASSET_CONF_FILE) in self.files_by_path:
                    self.check_asset_conf(asset_directory / fmt.ASSET_CONF_FILE)
                if (asset_directory / fmt.UPSTREAM_CONF_FILE) in self.files_by_path:
                    self.check_upstream_conf(asset_directory / fmt.UPSTREAM_CONF_FILE)

    def check_asset_conf(self, relative_path: Path) -> None:
        record = self.files_by_path[relative_path]
        if record.text is None:
            return  # the walk reported why the file was not read
        document = parse_key_value_text(record.text)
        for message in document.syntax_errors():
            self.refuse(relative_path, "metadata.asset-conf", message)
        if document.get("format").strip() != str(fmt.FORMAT_VERSION):
            self.refuse(relative_path, "metadata.asset-conf", f"`format={document.get('format')}`; this format is `{fmt.FORMAT_VERSION}`")
        for key in fmt.ASSET_CONF_LIST_KEYS:
            if not document.has(key):
                continue
            items, reason = document.get_list(key)
            if reason is not None:
                self.refuse(relative_path, "metadata.asset-conf", f"`{key}` is not a list ({reason})")
                continue
            for item in items:
                if key == "requires_capabilities" and item not in fmt.KNOWN_CAPABILITIES:
                    self.refuse(relative_path, "metadata.asset-conf", f"`{item}` is not a capability this format defines; the asset is refused")
                elif key == "requires_integrations" and not fmt.INTEGRATION_TOKEN_PATTERN.match(item):
                    self.refuse(relative_path, "metadata.asset-conf", f"`{item}` is not written as integration-<name>")
        self.check_known_keys(relative_path, document, set(fmt.ASSET_CONF_REQUIRED) | set(fmt.ASSET_CONF_OPTIONAL))

    def check_upstream_conf(self, relative_path: Path) -> None:
        record = self.files_by_path[relative_path]
        if record.text is None:
            return  # the walk reported why the file was not read
        document = parse_key_value_text(record.text)
        for message in document.syntax_errors():
            self.refuse(relative_path, "provenance.syntax", message)
        missing = [key for key in fmt.UPSTREAM_CONF_REQUIRED if not document.get(key).strip()]
        if missing:
            self.refuse(relative_path, "provenance.syntax", "missing or empty: " + ", ".join(f"`{key}=`" for key in missing))
        revision = document.get("revision").strip()
        if revision and not fmt.COMMIT_ID_PATTERN.match(revision):
            self.refuse(relative_path, "provenance.syntax", f"`revision={revision}` is not a full commit id")
        self.check_known_keys(relative_path, document, set(fmt.UPSTREAM_CONF_REQUIRED) | set(fmt.UPSTREAM_CONF_OPTIONAL))
        if document.get("license").strip():
            evaluation = check_declared_license(self.collector, self.display(relative_path), document.get("license"),
                                                self.options.license_allowlist, "the upstream licence")
            if evaluation.is_allowed:
                self.summary.declared_licenses.update(evaluation.identifiers)
                check_license_texts(self.collector, self.display(relative_path), evaluation.identifiers,
                                    self.carried_license_texts(relative_path.parent), self.options.license_text_directories)

    def check_reserved_kind_directories(self) -> None:
        for kind in fmt.KINDS:
            if kind.support != "reserved" or not kind.directory:
                continue
            directory = Path(kind.directory)
            if directory in self.directories and directory.name not in fmt.SET_ROOT_ENTRIES_RESERVED:
                self.refuse(directory, "kind.reserved", f"`{kind.directory}/` is the reserved kind `{kind.kind_id}`, which this format does not allow")

    # ── release profile ────────────────────────────────────────────────────────────────────────────────────────
    def check_release_inventory(self) -> None:
        inventory_path = Path("SHA256SUMS")
        record = self.files_by_path.get(inventory_path)
        if record is None or record.text is None:
            self.refuse(inventory_path, "release.inventory", "a built set carries SHA256SUMS at its root")
            return
        listed: Dict[str, str] = {}
        for line_number, line in enumerate(record.text.splitlines(), start=1):
            match = SHA256SUMS_LINE.match(line)
            if not match:
                self.refuse(inventory_path, "release.inventory", f"line {line_number} is not `<sha256>  <path>`")
                continue
            if match.group("path") in listed:
                self.refuse(inventory_path, "release.inventory", f"line {line_number} lists `{match.group('path')}` again; a file is listed once")
                continue
            listed[match.group("path")] = match.group("digest")
        present = {str(path): file for path, file in self.files_by_path.items() if path.name not in ("SHA256SUMS", "SHA256SUMS.asc")}
        for missing in sorted(set(present) - set(listed)):
            self.refuse(inventory_path, "release.inventory", f"`{missing}` is in the set and not in SHA256SUMS")
        for extra in sorted(set(listed) - set(present)):
            self.refuse(inventory_path, "release.inventory", f"`{extra}` is in SHA256SUMS and not in the set")
        for path_text, digest in sorted(listed.items()):
            if path_text not in present:
                continue
            if present[path_text].digest is None:
                self.refuse(inventory_path, "release.inventory", f"`{path_text}` was not read whole, so its SHA256SUMS line cannot be checked")
            elif present[path_text].digest != digest:
                self.refuse(inventory_path, "release.inventory", f"`{path_text}` does not match its SHA256SUMS line")


def read_reserved_words(path: Path) -> Set[str]:
    """The words a set name does not start with, one per line, `#` lines and blanks skipped."""
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")}
