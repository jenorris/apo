"""Note format helpers — Markdown vs catalog YAML records.

Markdown remains the prose substrate (headings, append, hybrid search body).
``.yaml`` / ``.yml`` notes are whole-file mappings indexed as ``files.frontmatter``
with optional title/description search chunks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

NOTE_SUFFIXES = frozenset({".md", ".yaml", ".yml"})
YAML_SUFFIXES = frozenset({".yaml", ".yml"})
MARKDOWN_SUFFIXES = frozenset({".md"})

# Default ignore extras so machine contracts are not catalog noise.
DEFAULT_YAML_IGNORE = (
    "system/contracts/*-contract.schema.yaml",
    "system/contracts/*-contract.schema.yml",
    "system/contracts/*.schema.yaml",
    "system/contracts/*.schema.yml",
    "system/config/*-contract.schema.yaml",
    "system/config/*-contract.schema.yml",
    "system/config/okf-profile.schema.yaml",
    "system/config/okf-profile.schema.yml",
)


def _construct_yaml_timestamp(loader: yaml.SafeLoader, node: yaml.Node):
    try:
        return yaml.constructor.SafeConstructor.construct_yaml_timestamp(loader, node)
    except (ValueError, OverflowError):
        return loader.construct_scalar(node)


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that tolerates invalid YAML 1.1 timestamp scalars."""


_TolerantLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    _construct_yaml_timestamp,
)


def suffix_of(path: str | Path) -> str:
    return Path(str(path).replace("\\", "/")).suffix.lower()


def is_note_path(path: str | Path) -> bool:
    return suffix_of(path) in NOTE_SUFFIXES


def is_yaml_note(path: str | Path) -> bool:
    return suffix_of(path) in YAML_SUFFIXES


def is_markdown_note(path: str | Path) -> bool:
    return suffix_of(path) in MARKDOWN_SUFFIXES


def ensure_indexed_path(rel_path: str) -> str:
    """Normalize a vault-relative path for index lookup.

    Bare stems (no suffix) still default to ``.md`` for back-compat with
    ``frontmatter_field`` / history callers that omit the extension.
    """
    rel = rel_path.replace("\\", "/").lstrip("/")
    if is_note_path(rel):
        return rel
    return f"{rel}.md"


def parse_yaml_document(text: str) -> dict[str, Any] | None:
    """Parse a standalone YAML document. Mapping → dict; else None.

    Round-trip (comment-preserving) parse first — the result is a ``CommentedMap``,
    a ``dict`` subclass, so catalog/filter consumers see no difference. Documents
    ruamel refuses fall back to the tolerant PyYAML loader, as before.
    """
    from apo_engine import yaml_rt

    rt = yaml_rt.load(text)
    if rt is not None:
        return rt
    try:
        data = yaml.load(text, Loader=_TolerantLoader)
    except (yaml.YAMLError, ValueError, OverflowError):
        return None
    return data if isinstance(data, dict) else None


def dump_yaml_document(data: dict[str, Any]) -> str:
    """Serialize a mapping as a vault YAML note (trailing newline).

    Comments and formatting carried by a round-tripped mapping survive; a plain
    dict emits as it always has.
    """
    from apo_engine import yaml_rt

    return yaml_rt.dump(data)


def yaml_search_chunk_text(fm: dict[str, Any] | None, rel: str) -> str:
    """Build embed/FTS text from catalog fields (phase 3)."""
    parts: list[str] = []
    src = fm if isinstance(fm, dict) else {}
    for key in ("title", "description", "summary", "okf_type", "status", "resource"):
        val = src.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, (dict, list)):
            continue
        parts.append(f"{key}: {val}")
    if parts:
        return "\n".join(parts)
    stem = Path(rel).stem.replace("-", " ").replace("_", " ").strip()
    return stem or rel


def chunk_yaml_note(
    text: str,
    rel: str,
    fm: dict[str, Any] | None,
) -> list[tuple[str, int, str, int, int]]:
    """One search chunk covering the whole YAML file (line span 1..N)."""
    search = yaml_search_chunk_text(fm, rel)
    if not text:
        end = 1
    else:
        end = text.count("\n") + (0 if text.endswith("\n") else 1)
        end = max(1, end)
    return [("", 0, search, 1, end)]


def coerce_yaml_value(value: Any) -> Any:
    """Coerce patch ``value`` strings through YAML when possible."""
    if not isinstance(value, str):
        return value
    try:
        return yaml.load(value, Loader=_TolerantLoader)
    except (yaml.YAMLError, ValueError, OverflowError):
        return value
