"""Note format helpers — Markdown and structured catalog records.

Markdown remains the prose substrate (headings, append, hybrid search body).
``.yaml`` / ``.yml`` notes are whole-file mappings indexed as ``files.frontmatter``
with optional title/description search chunks. Top-level JSON objects use the
same catalog model while preserving their raw bytes on whole-file writes.
JSON arrays and scalars are valid payloads but are not catalog records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

NOTE_SUFFIXES = frozenset({".md", ".yaml", ".yml", ".mmd", ".json"})
# Watcher heartbeat and git-sync status. Not notes, even though ``.json`` is a note suffix.
ENGINE_STATE_DIR = ".apo"
ENGINE_STATE_GLOBS = (".apo/*",)
YAML_SUFFIXES = frozenset({".yaml", ".yml"})
JSON_SUFFIXES = frozenset({".json"})
MARKDOWN_SUFFIXES = frozenset({".md"})
MMD_SUFFIXES = frozenset({".mmd"})
MUTATOR_SUFFIXES = NOTE_SUFFIXES | JSON_SUFFIXES

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


def is_contract_schema_path(path: str | Path) -> bool:
    """True for machine contract/schema YAML under system/contracts or system/config."""
    rel = str(path).replace("\\", "/").lstrip("/")
    if not is_yaml_note(rel):
        return False
    if not (rel.startswith("system/contracts/") or rel.startswith("system/config/")):
        return False
    name = Path(rel).name.lower()
    return (
        name.endswith(".schema.yaml")
        or name.endswith(".schema.yml")
        or "-contract.schema." in name
        or name.startswith("okf-profile.schema.")
    )


def is_note_path(path: str | Path) -> bool:
    return suffix_of(path) in NOTE_SUFFIXES


def has_blocked_dot_segment(rel: str) -> bool:
    """True for ``.git``, ``.env``, or ``.apo`` path segments.

    ``..`` is left to lexical normalization so a contract-schema alias still
    reaches the protected-control-plane check.
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    return any(p in {".git", ".env", ENGINE_STATE_DIR} or p.startswith(".env") for p in parts)


def resolved_target_exposed(root: Path, path: Path) -> bool:
    """True when ``path``, followed through symlinks, stays in ``root`` off blocked dot-segments.

    The lexical checks above see only the name asked for; an in-vault symlink
    such as ``leak.md -> .env`` passes them while its target does not.
    """
    try:
        rel = Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return False
    return not has_blocked_dot_segment(rel)


def is_exposed_note_rel(rel: str) -> bool:
    """True when a vault-relative path is a note and not under a dot-segment."""
    if has_blocked_dot_segment(rel):
        return False
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return False
    return is_note_path(parts[-1])


def is_yaml_note(path: str | Path) -> bool:
    return suffix_of(path) in YAML_SUFFIXES


def is_markdown_note(path: str | Path) -> bool:
    return suffix_of(path) in MARKDOWN_SUFFIXES


def is_mmd_note(path: str | Path) -> bool:
    return suffix_of(path) in MMD_SUFFIXES


def is_json_catalog(path: str | Path) -> bool:
    return suffix_of(path) in JSON_SUFFIXES


def is_mutator_path(path: str | Path) -> bool:
    return suffix_of(path) in MUTATOR_SUFFIXES


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


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse a top-level JSON object; return ``None`` for invalid or non-object JSON."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
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


def project_large_json(data: dict[str, Any]) -> dict[str, Any]:
    """Top-level scalars only. Nested objects and arrays become a length marker.

    A multi-megabyte export (IAM dumps, CloudWatch inventories) must not become
    one FTS document or one embed request. Small objects never take this path.
    """
    projected: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            projected[key] = {"_omitted": "object", "len": len(val)}
        elif isinstance(val, list):
            projected[key] = {"_omitted": "array", "len": len(val)}
        elif isinstance(val, str) and len(val) > 500:
            projected[key] = val[:500] + "…"
        else:
            projected[key] = val
    return projected


def json_index_documents(data: dict[str, Any], *, max_chars: int) -> tuple[str, str]:
    """``(search_chunk_text, frontmatter_json)`` for one JSON catalog note.

    Under ``max_chars`` both are the full document (search text is key-sorted).
    Over the cap both are :func:`project_large_json`. ``max_chars <= 0`` disables
    the cap. The search string is hard-cut if the projection itself is still long.
    """
    searchable = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    fm = json.dumps(data, ensure_ascii=False, default=str)
    if max_chars <= 0 or (len(searchable) <= max_chars and len(fm) <= max_chars):
        return searchable, fm
    projected = project_large_json(data)
    searchable = json.dumps(projected, ensure_ascii=False, sort_keys=True, default=str)
    fm = json.dumps(projected, ensure_ascii=False, default=str)
    if len(searchable) > max_chars:
        searchable = searchable[:max_chars]
    if len(fm) > max_chars:
        fm = json.dumps({"_apo_omitted": "json exceeds index cap"}, ensure_ascii=False)
    return searchable, fm


def chunk_json_note(
    text: str,
    rel: str,
    data: dict[str, Any],
) -> list[tuple[str, int, str, int, int]]:
    """One searchable chunk for a JSON object.

    Objects that serialize under ``JSON_CHUNK_MAX_CHARS`` keep every key and
    value. Larger ones store a scalar projection so the chunk cannot be megabytes.
    """
    from apo_engine import config

    searchable, _fm = json_index_documents(data, max_chars=config.JSON_CHUNK_MAX_CHARS)
    end = max(1, text.count("\n") + (0 if text.endswith("\n") else 1))
    return [("", 0, searchable, 1, end)]


def coerce_yaml_value(value: Any) -> Any:
    """Coerce patch ``value`` strings through YAML when possible."""
    if not isinstance(value, str):
        return value
    try:
        return yaml.load(value, Loader=_TolerantLoader)
    except (yaml.YAMLError, ValueError, OverflowError):
        return value
