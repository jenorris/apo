"""In-memory markdown → HTML render for local-web (no adjacent .html writes).

Includes a process-local LRU cache keyed by source + asset mtimes so repeat
views skip pandoc. Cache entries invalidate automatically when the note (or
stylesheet/template/header) changes on disk.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from apo_engine import local_web_contract as lwc
from apo_engine import ofm_unwrap

_BUNDLE = Path(__file__).resolve().parent / "assets" / "htmlize"

# Path → (key, result). OrderedDict for simple LRU eviction.
_CACHE: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 64


class RenderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_asset(vault_root: Path, rel: str, bundled_name: str) -> Path:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if rel:
        candidate = (vault_root / rel).resolve()
        try:
            candidate.relative_to(vault_root.resolve())
        except ValueError:
            candidate = Path()  # force fallback
        else:
            if candidate.is_file():
                return candidate
    bundled = _BUNDLE / bundled_name
    if bundled.is_file():
        return bundled
    raise RenderError("missing_asset", f"render asset not found: {bundled_name}")


def resolve_assets(vault_root: Path, render: dict[str, Any]) -> dict[str, Path]:
    return {
        "stylesheet": _resolve_asset(
            vault_root, str(render.get("stylesheet") or ""), "htmlize.css"
        ),
        "template": _resolve_asset(
            vault_root, str(render.get("template") or ""), "htmlize.template.html"
        ),
        "header": _resolve_asset(
            vault_root, str(render.get("header") or ""), "htmlize-header.html"
        ),
    }


def safe_note_path(vault_root: Path, rel_path: str) -> Path:
    rel = rel_path.replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise RenderError("bad_path", "invalid path")
    if not rel.endswith(".md"):
        raise RenderError("bad_path", "expected .md path")
    abs_path = (vault_root / rel).resolve()
    try:
        abs_path.relative_to(vault_root.resolve())
    except ValueError as e:
        raise RenderError("bad_path", "path escapes vault root") from e
    if not abs_path.is_file():
        raise RenderError("not_found", f"note not found: {rel}")
    return abs_path


def note_mtime_ns(vault_root: Path, rel_path: str) -> int:
    """Source file mtime in ns (for hot-reload polling)."""
    return safe_note_path(vault_root, rel_path).stat().st_mtime_ns


def _file_stamp(path: Path) -> str:
    try:
        st = path.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "missing"


def _cache_key(
    root: Path,
    rel: str,
    layout: str | None,
    abs_path: Path,
    assets: dict[str, Path],
    render_cfg: dict[str, Any],
) -> str:
    parts = [
        str(root),
        rel,
        layout or "",
        _file_stamp(abs_path),
        _file_stamp(assets["stylesheet"]),
        _file_stamp(assets["template"]),
        _file_stamp(assets["header"]),
        str(render_cfg.get("default_layout") or ""),
        str(render_cfg.get("resolve_wikilinks") or ""),
        str(bool(render_cfg.get("strip_frontmatter", True))),
    ]
    return hashlib.blake2b("|".join(parts).encode(), digest_size=16).hexdigest()


def clear_render_cache(rel_path: str | None = None) -> int:
    """Drop all cache entries, or those for one vault-relative path. Returns count removed."""
    with _CACHE_LOCK:
        if rel_path is None:
            n = len(_CACHE)
            _CACHE.clear()
            return n
        needle = rel_path.replace("\\", "/").lstrip("/")
        drop = [k for k, (_ck, result) in _CACHE.items() if result.get("path") == needle]
        for k in drop:
            del _CACHE[k]
        return len(drop)


def cache_stats() -> dict[str, int]:
    with _CACHE_LOCK:
        return {"entries": len(_CACHE), "max": _CACHE_MAX}


def _cache_get(slot: str, key: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        item = _CACHE.get(slot)
        if item is None:
            return None
        cached_key, result = item
        if cached_key != key:
            return None
        _CACHE.move_to_end(slot)
        out = dict(result)
        out["cached"] = True
        return out


def _cache_put(slot: str, key: str, result: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[slot] = (key, dict(result))
        _CACHE.move_to_end(slot)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def render_note_html(
    vault_root: Path,
    rel_path: str,
    *,
    layout: str | None = None,
    contract: dict[str, Any] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Render a vault note to HTML bytes in memory.

    Returns ``{ok, html, path, layout, title, mtime_ns, cached}``.
    Never writes adjacent ``.html``.
    """
    root = vault_root.resolve()
    data = contract if contract is not None else lwc.load_local_web_contract(root)
    if data is None:
        raise RenderError("no_contract", "local-web contract not active")

    allowed, reason = lwc.is_path_allowed(root, rel_path, data=data)
    if not allowed:
        raise RenderError("forbidden", reason)

    abs_path = safe_note_path(root, rel_path)
    render_cfg = lwc.resolve_render(data)
    assets = resolve_assets(root, render_cfg)
    rel = rel_path.replace("\\", "/").lstrip("/")
    mtime_ns = abs_path.stat().st_mtime_ns

    slot = f"{root}:{rel}:{layout or ''}"
    key = _cache_key(root, rel, layout, abs_path, assets, render_cfg)
    if use_cache:
        hit = _cache_get(slot, key)
        if hit is not None:
            hit["mtime_ns"] = mtime_ns
            return hit

    if not shutil.which("pandoc"):
        raise RenderError("pandoc_missing", "pandoc not on PATH (brew install pandoc)")

    raw = abs_path.read_text(encoding="utf-8")

    def _allowed(hint: str) -> bool:
        ok, _ = lwc.is_path_allowed(root, hint, data=data)
        if not ok:
            return False
        try:
            safe_note_path(root, hint if hint.endswith(".md") else hint + ".md")
            return True
        except RenderError:
            return ok

    meta, resolved_layout, body = ofm_unwrap.build_gfm_body(
        raw,
        abs_path,
        cli_layout=layout,
        default_layout=str(render_cfg.get("default_layout") or "memo"),
        strip_fm=bool(render_cfg.get("strip_frontmatter", True)),
        resolve_wikilinks=str(render_cfg.get("resolve_wikilinks") or "plain"),
        path_allowed=_allowed if render_cfg.get("resolve_wikilinks") == "serve-routes" else None,
    )
    pmeta = ofm_unwrap.pandoc_metadata(meta, resolved_layout)

    with tempfile.TemporaryDirectory(prefix="apo-render-") as tmp:
        tmp_path = Path(tmp)
        body_file = tmp_path / "body.md"
        meta_file = tmp_path / "meta.json"
        body_file.write_text(body, encoding="utf-8")
        meta_file.write_text(json.dumps(pmeta, indent=2) + "\n", encoding="utf-8")

        cmd = [
            "pandoc",
            "-f",
            "gfm",
            "-t",
            "html5",
            "--standalone",
            "--embed-resources",
            f"--template={assets['template']}",
            "--toc",
            "--toc-depth=3",
            f"--metadata-file={meta_file}",
            f"--css={assets['stylesheet']}",
        ]
        if assets["header"].is_file() and assets["header"].stat().st_size > 0:
            cmd.append(f"--include-in-header={assets['header']}")
        cmd.extend(["-o", "-", str(body_file)])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RenderError("pandoc_failed", str(e)) from e
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            err = (proc.stderr or proc.stdout or "pandoc failed").strip()
            raise RenderError("pandoc_failed", err[:500])
        html = proc.stdout

    result = {
        "ok": True,
        "html": html,
        "path": rel,
        "layout": resolved_layout,
        "title": meta.get("title") or abs_path.stem,
        "mtime_ns": mtime_ns,
        "cached": False,
    }
    if use_cache:
        _cache_put(slot, key, result)
    return result
