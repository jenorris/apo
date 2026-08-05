#!/usr/bin/env python3
"""Shared helpers for OKF vault tools (stdlib only — no PyYAML).

VAULT_ROOT is configured via configure() / --vault / VAULT_ROOT env — never
inferred from this package's install path.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT: Path = Path(".")  # set via configure() before use

SKIP_DIR_NAMES = {
    ".git",
    ".obsidian",
    ".cursor",
    "node_modules",
    "__pycache__",
    ".agents",
    "system/agent-client",
}

RESERVED = {"index.md", "log.md"}
PARA_ROOTS = ("inbox", "projects", "areas", "resources", "archives", "system")

FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
H1_RE = re.compile(r"(?m)^#\s+(.+)$")
SCALAR_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")


def configure(vault_root: Path) -> Path:
    """Set module-global VAULT_ROOT and return it."""
    global VAULT_ROOT
    VAULT_ROOT = vault_root.resolve()
    return VAULT_ROOT


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel_to_vault(path: Path) -> str:
    try:
        return path.resolve().relative_to(VAULT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def bundle_url(path: Path) -> str:
    return "/" + rel_to_vault(path)


def should_skip_dir(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIR_NAMES:
        return True
    if "agent-client" in path.parts:
        return True
    return False


def iter_markdown(root: Path | None = None) -> list[Path]:
    base = (root or VAULT_ROOT).resolve()
    out: list[Path] = []
    if base.is_file() and base.suffix == ".md":
        return [base]
    if not base.exists():
        return []
    for path in sorted(base.rglob("*.md")):
        if should_skip_dir(path.parent):
            continue
        out.append(path)
    return out


def split_frontmatter(text: str) -> tuple[dict[str, str], str, bool]:
    """Return (scalars, body, has_frontmatter). Multilines ignored as scalars."""
    match = FM_RE.match(text)
    if not match:
        return {}, text, False
    block, body = match.group(1), match.group(2)
    scalars: dict[str, str] = {}
    for line in block.splitlines():
        if not line or line.startswith((" ", "\t", "-", "#")):
            continue
        m = SCALAR_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        scalars[key] = val
    return scalars, body, True


def first_heading(body: str) -> str | None:
    m = H1_RE.search(body)
    return m.group(1).strip() if m else None


def is_bundle_root_index(path: Path) -> bool:
    return path.resolve() == (VAULT_ROOT / "index.md").resolve()


def is_reserved(path: Path) -> bool:
    return path.name in RESERVED


def map_okf_type(path: Path, meta_type: str | None, existing: str | None) -> str | None:
    if existing:
        return existing
    rel = rel_to_vault(path)
    t = (meta_type or "").strip()
    if t == "project" or (t == "index" and rel.startswith("projects/") and path.name != "index.md"):
        return "Project"
    if t == "area" or rel.startswith("areas/") and path.name.endswith(".md") and "/threads/" not in rel:
        if "/goals/" in rel:
            return "Goal"
        if path.parent.name == "areas" or rel.count("/") == 1:
            return "Area"
    if t == "thread" or "/threads/" in rel:
        return "Thread"
    if t == "spec":
        return "Specification"
    if t in {"template", "prompt", "persona"} or rel.startswith("system/"):
        if t in {"template", "prompt", "persona"}:
            return "System"
    if t == "resource" or rel.startswith("resources/"):
        if "/wiki/" in rel:
            return "Concept"
        return "Reference"
    if t == "log":
        if "/daily/" in rel:
            return "Journal"
        if "/tasks/" in rel:
            return "Task"
        if "/zettels/" in rel:
            return "Capture"
    if rel.startswith("projects/pci-2026/") and path.name == "status.md":
        return "EvidenceRequest"
    if rel.startswith("projects/pci-2026/") and ("evidence" in path.name or "tra-" in path.name):
        return "EvidenceArtifact"
    if rel.startswith("areas/compliance/obligations/") and path.name.startswith("obl-"):
        return "Obligation"
    if rel.startswith("areas/compliance/policies/") and path.name not in {
        "README.md",
        "policies.md",
        "index.md",
    }:
        return "Policy"
    if path.name == "cluster-index.md":
        return "Collection"
    if t == "note" and "/wiki/" in rel:
        return "Concept"
    return "Note"


def set_scalar_in_frontmatter(text: str, updates: dict[str, str]) -> str:
    """Insert or replace scalar frontmatter keys; create FM if missing."""
    match = FM_RE.match(text)
    if not match:
        lines = ["---"]
        for key, val in updates.items():
            lines.append(f"{key}: {_yaml_quote(val)}")
        lines.append("---")
        body = text if text.startswith("\n") else "\n" + text
        return "\n".join(lines) + body

    block, body = match.group(1), match.group(2)
    lines = block.splitlines()
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        m = SCALAR_RE.match(line) if line and not line.startswith((" ", "\t", "-")) else None
        if m and m.group(1) in updates:
            key = m.group(1)
            new_lines.append(f"{key}: {_yaml_quote(updates[key])}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            new_lines.append(f"{key}: {_yaml_quote(val)}")
    return "---\n" + "\n".join(new_lines) + "\n---\n" + body.lstrip("\n")


def strip_frontmatter(text: str) -> str:
    match = FM_RE.match(text)
    if not match:
        return text
    body = match.group(2)
    return body.lstrip("\n") if body else ""


def _yaml_quote(val: str) -> str:
    if val == "":
        return '""'
    if (
        re.search(r'[:#\[\]{}&*!|,>"\']', val)
        or val.lower() in {"true", "false", "null", "yes", "no"}
        or " " in val
        or "/" in val
    ):
        return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return val


def concept_title(path: Path, scalars: dict[str, str], body: str) -> str:
    return scalars.get("title") or first_heading(body) or path.stem


def concept_description(scalars: dict[str, str], body: str) -> str | None:
    if scalars.get("description"):
        return scalars["description"]
    return first_heading(body)
