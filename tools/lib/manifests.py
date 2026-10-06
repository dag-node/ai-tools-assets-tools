# SPDX-License-Identifier: MIT
"""The plugin manifests and the marketplace a publisher repository commits, rendered from set.conf and publisher.conf.

Each set is a Claude Code plugin and an Agent Plugins 1.0 plugin as committed, so its two manifests and the
repository's marketplace are written from the one source of a set's name, version, summary and licence, and CI refuses
a committed copy that differs from what this module renders. The key order and the two-space indent are part of the
rendering, since `sync-manifests --check` compares bytes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from asset_format import (CLAUDE_PLUGIN_DIRECTORY, CLAUDE_PLUGIN_MANIFEST, FILE_MAX_BYTES, MARKETPLACE_NAME_SUFFIX,
                          PLUGIN_NAME_PREFIX, PORTABLE_PLUGIN_MANIFEST, PORTABLE_PLUGIN_SCHEMA)
from key_value_config import KeyValueDocument
from safe_read import RefusedRead, read_text_under

MARKETPLACE_PATH = Path(".claude-plugin") / "marketplace.json"


def plugin_name(set_name: str) -> str:
    return PLUGIN_NAME_PREFIX + set_name


def author_document(publisher: KeyValueDocument) -> Dict[str, str]:
    return {"name": publisher.get("publisher"), "email": publisher.get("contact")}


def portable_plugin_document(set_conf: KeyValueDocument, publisher: KeyValueDocument) -> Dict[str, object]:
    return {
        "$schema": PORTABLE_PLUGIN_SCHEMA,
        "name": plugin_name(set_conf.get("name")),
        "version": set_conf.get("version"),
        "description": set_conf.get("summary"),
        "author": author_document(publisher),
        "homepage": publisher.get("source"),
        "repository": publisher.get("source"),
        "license": set_conf.get("license"),
    }


def claude_plugin_document(set_conf: KeyValueDocument, publisher: KeyValueDocument) -> Dict[str, object]:
    document = portable_plugin_document(set_conf, publisher)
    del document["$schema"]
    return document


def marketplace_document(publisher: KeyValueDocument, sets: Sequence[Tuple[str, KeyValueDocument]]) -> Dict[str, object]:
    return {
        "name": publisher.get("publisher") + MARKETPLACE_NAME_SUFFIX,
        "owner": author_document(publisher),
        "description": publisher.get("description"),
        "plugins": [
            {
                "name": plugin_name(set_name),
                "source": f"./sets/{set_name}",
                "description": set_conf.get("summary"),
                "license": set_conf.get("license"),
            }
            for set_name, set_conf in sorted(sets, key=lambda pair: pair[0])
        ],
    }


def render_json(document: Dict[str, object]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def expected_manifest_files(publisher: KeyValueDocument, sets: Sequence[Tuple[str, KeyValueDocument]]) -> Dict[Path, str]:
    """Every generated file of a repository, keyed by its path relative to the repository root."""
    files: Dict[Path, str] = {MARKETPLACE_PATH: render_json(marketplace_document(publisher, sets))}
    for set_name, set_conf in sets:
        set_directory = Path("sets") / set_name
        files[set_directory / PORTABLE_PLUGIN_MANIFEST] = render_json(portable_plugin_document(set_conf, publisher))
        files[set_directory / CLAUDE_PLUGIN_DIRECTORY / CLAUDE_PLUGIN_MANIFEST] = render_json(
            claude_plugin_document(set_conf, publisher))
    return files


def stale_manifest_files(root_fd: int, expected: Dict[Path, str]) -> List[Tuple[Path, str]]:
    """The expected files whose committed copy under `root_fd` is absent, unreadable or differs, each with the reason."""
    stale: List[Tuple[Path, str]] = []
    for relative_path, text in expected.items():
        try:
            committed = read_text_under(root_fd, relative_path, FILE_MAX_BYTES)
        except FileNotFoundError:
            stale.append((relative_path, "is absent"))
            continue
        except RefusedRead as refusal:
            stale.append((relative_path, refusal.message))
            continue
        if committed != text:
            stale.append((relative_path, "differs from what sync-manifests writes"))
    return stale
