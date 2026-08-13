"""Local-web contract loader — localhost desk viewer + on-the-fly HTML.

Active when ``system/contracts/local-web-contract.schema.yaml`` (or legacy
``system/config/local-web-contract.schema.yaml``) exists under the vault root.
Env override: ``APO_LOCAL_WEB_CONTRACT``.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

import yaml

LOCAL_WEB_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "local-web-contract.schema.yaml",
    Path("system") / "config" / "local-web-contract.schema.yaml",
)
LOCAL_WEB_CONTRACT_REL = LOCAL_WEB_CONTRACT_CANDIDATES[0]

_PARA_ROOTS = ("projects", "areas", "resources", "inbox", "archives")
_WIKI_ROOTS = ("wiki", "raw")
_DEFAULT_EXCLUDE = (
    "inbox/daily/**",
    "system/agent-client/**",
    "system/audit/**",
    "**/audit/resolved/**",
    "**/.git/**",
    "**/node_modules/**",
)
_DEFAULT_FEATURES = {
    "search": True,
    "filter": True,
    "backlinks": True,
    "mermaid": True,
    "math": True,
}
_DEFAULT_RENDER = {
    "engine": "pandoc",
    "default_layout": "memo",
    "stylesheet": "system/assets/htmlize.css",
    "template": "system/assets/htmlize.template.html",
    "header": "system/assets/htmlize-header.html",
    "strip_frontmatter": True,
    "resolve_wikilinks": "serve-routes",
}


def resolve_local_web_contract_path(
    vault_root: Path, explicit: str | None = None
) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_LOCAL_WEB_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in LOCAL_WEB_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_local_web_contract(
    vault_root: Path, explicit: str | None = None
) -> dict[str, Any] | None:
    """Parse local-web-contract YAML if present. Returns None when missing/unreadable."""
    path = resolve_local_web_contract_path(vault_root, explicit)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def local_web_contract_active(vault_root: Path) -> bool:
    return load_local_web_contract(vault_root) is not None


def _normalize_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip().replace("\\", "/").strip("/"))
    return out


def _dir_exists(vault_root: Path, name: str) -> bool:
    return (vault_root / name).is_dir()


def detect_layout_mode(vault_root: Path) -> str:
    """Return ``para``, ``llm-wiki``, ``mixed``, or ``none`` from on-disk layout."""
    para = any(_dir_exists(vault_root, n) for n in _PARA_ROOTS)
    wiki = _dir_exists(vault_root, "wiki") and _dir_exists(vault_root, "raw")
    if para and wiki:
        return "mixed"
    if wiki:
        return "llm-wiki"
    if para:
        return "para"
    return "none"


def derive_browse_roots(vault_root: Path, mode: str) -> list[str]:
    """Derive browse roots from co-active layout signals."""
    mode = (mode or "adaptive").strip().lower() or "adaptive"
    roots: list[str] = []
    want_para = mode in ("adaptive", "para", "mixed")
    want_wiki = mode in ("adaptive", "llm-wiki", "mixed")
    if mode == "adaptive":
        detected = detect_layout_mode(vault_root)
        want_para = detected in ("para", "mixed")
        want_wiki = detected in ("llm-wiki", "mixed")
        if detected == "none":
            # Fall back to any present PARA-ish or wiki dirs individually.
            want_para = True
            want_wiki = True
    if want_para:
        for name in _PARA_ROOTS:
            if _dir_exists(vault_root, name):
                roots.append(name)
    if want_wiki:
        for name in _WIKI_ROOTS:
            if _dir_exists(vault_root, name):
                roots.append(name)
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def resolve_browse_roots(vault_root: Path, data: dict[str, Any] | None = None) -> list[str]:
    """Explicit ``browse_roots`` wins; else derive from ``mode`` + disk layout."""
    if data is None:
        data = load_local_web_contract(vault_root) or {}
    explicit = _normalize_str_list(data.get("browse_roots"))
    if explicit:
        return explicit
    mode = str(data.get("mode") or "adaptive").strip() or "adaptive"
    return derive_browse_roots(vault_root, mode)


def resolve_exclude_globs(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return list(_DEFAULT_EXCLUDE)
    raw = data.get("exclude_globs")
    if raw is None:
        return list(_DEFAULT_EXCLUDE)
    return _normalize_str_list(raw) or list(_DEFAULT_EXCLUDE)


def resolve_features(data: dict[str, Any] | None) -> dict[str, bool]:
    out = dict(_DEFAULT_FEATURES)
    if not data:
        return out
    features = data.get("features")
    if isinstance(features, dict):
        for key in out:
            if key in features:
                out[key] = bool(features[key])
    return out


def resolve_render(data: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(_DEFAULT_RENDER)
    if not data:
        return out
    render = data.get("render")
    if not isinstance(render, dict):
        return out
    for key, default in _DEFAULT_RENDER.items():
        if key not in render:
            continue
        val = render[key]
        if isinstance(default, bool):
            out[key] = bool(val)
        elif isinstance(val, str) and val.strip():
            out[key] = val.strip()
        elif val is not None and not isinstance(default, bool):
            out[key] = val
    return out


def resolve_bind_port(data: dict[str, Any] | None) -> tuple[str, int]:
    bind = "127.0.0.1"
    port = 7432
    if data:
        b = str(data.get("bind") or "").strip()
        if b:
            bind = b
        try:
            port = int(data.get("port") or port)
        except (TypeError, ValueError):
            port = 7432
    # Safety: never silently bind all interfaces in v1.
    if bind in ("0.0.0.0", "::", "[::]"):
        bind = "127.0.0.1"
    return bind, port


def path_matches_globs(rel_path: str, globs: list[str]) -> bool:
    """True if vault-relative path matches any exclude-style glob."""
    rel = rel_path.replace("\\", "/").lstrip("/")
    for pattern in globs:
        pat = pattern.replace("\\", "/").lstrip("/")
        if fnmatch.fnmatch(rel, pat):
            return True
        # Also match basename-only patterns and trailing /**
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
    return False


def path_in_browse_roots(rel_path: str, roots: list[str]) -> bool:
    """True if path is under a browse root, or roots is empty (whole vault)."""
    if not roots:
        return True
    rel = rel_path.replace("\\", "/").lstrip("/")
    for root in roots:
        r = root.replace("\\", "/").strip("/")
        if not r:
            return True
        if rel == r or rel.startswith(r + "/"):
            return True
    return False


def is_path_allowed(
    vault_root: Path,
    rel_path: str,
    *,
    data: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). Checks browse_roots + exclude_globs."""
    if data is None:
        data = load_local_web_contract(vault_root)
    if data is None:
        return False, "local-web contract not active"
    rel = rel_path.replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return False, "invalid path"
    excludes = resolve_exclude_globs(data)
    if path_matches_globs(rel, excludes):
        return False, "excluded by exclude_globs"
    roots = resolve_browse_roots(vault_root, data)
    if not path_in_browse_roots(rel, roots):
        return False, "outside browse_roots"
    return True, "ok"


def clear_local_web_cache() -> None:
    """Reserved for future caching; currently a no-op."""
    return None
