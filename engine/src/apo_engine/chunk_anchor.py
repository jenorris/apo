"""Resolve search ``chunk_hash`` anchors for append/patch (path + heading).

Hashes are ephemeral index keys. When the hash is missing but the caller still
has ``path`` + ``heading`` from the search hit, fall back to heading and tip.
"""

from __future__ import annotations

from typing import Any

from apo_engine import core, vaults
from apo_engine.markdown_patch import _HEADING_RE, _normalize_heading, normalize_lines

STALE_HASH_TIP = (
    "chunk_hash was stale (not in index); "
    "fell back to heading — re-search for a fresh hash"
)


def format_chunk_heading(title: str, level: int) -> str:
    title = (title or "").strip()
    if not title:
        return ""
    level = int(level or 0)
    if level > 0:
        return f"{'#' * level} {title}"
    return title


def _innermost_heading(lines: list[str], probe_line: int) -> str:
    """Nearest markdown heading at or above ``probe_line`` (1-based)."""
    if probe_line < 1:
        probe_line = 1
    idx = min(probe_line, len(lines)) - 1
    for i in range(idx, -1, -1):
        m = _HEADING_RE.match(lines[i])
        if m:
            return format_chunk_heading(m.group(2).strip(), len(m.group(1)))
    return ""


def resolve_chunk_anchor(
    binding: vaults.VaultBinding,
    chunk_hash: str,
    *,
    path: str | None = None,
    heading_fallback: str | None = None,
) -> dict[str, Any]:
    """Resolve ``chunk_hash`` to ``{ok, path, heading, tip?}`` or an error dict.

    Optional ``path`` is a guard when the hash is found. On miss, ``path`` +
    ``heading_fallback`` enable a stale-hash heading retry with ``tip``.

    When the hash is found, ``heading`` is the innermost heading covering the
    chunk span (so nested ``##`` sections work with ``find_section``).
    """
    ch = (chunk_hash or "").strip()
    if not ch:
        return {
            "ok": False,
            "error": "bad_request",
            "message": "chunk_hash is empty",
        }

    want = (path or "").replace("\\", "/").strip() or None
    heading_fb = (heading_fallback or "").strip() or None

    with vaults.bind(binding):
        chunk = core.lookup_chunk(ch, include_text=False)

    if chunk:
        chunk_path = (chunk.get("path") or "").replace("\\", "/").strip()
        if not chunk_path:
            return {
                "ok": False,
                "error": "anchor_not_found",
                "message": f"chunk_hash {ch!r} has no path",
            }
        if want and chunk_path != want:
            return {
                "ok": False,
                "error": "path_mismatch",
                "message": f"chunk_hash belongs to {chunk_path!r}, not {want!r}",
                "path": want,
            }
        index_heading = format_chunk_heading(
            str(chunk.get("heading") or ""),
            int(chunk.get("heading_level") or 0),
        )
        heading = index_heading
        try:
            full = binding.resolved().root / chunk_path
            if full.is_file():
                lines = normalize_lines(full.read_text(encoding="utf-8"))
                probe = int(chunk.get("end_line") or chunk.get("start_line") or 1)
                refined = _innermost_heading(lines, probe)
                if refined:
                    heading = refined
        except OSError:
            pass

        if heading_fb and index_heading:
            fb_n = _normalize_heading(heading_fb)
            if fb_n != _normalize_heading(index_heading) and (
                not heading or fb_n != _normalize_heading(heading)
            ):
                return {
                    "ok": False,
                    "error": "bad_request",
                    "message": (
                        f"conflicting chunk_hash heading {index_heading!r} "
                        f"and heading {heading_fb!r}"
                    ),
                    "path": chunk_path,
                }
        return {
            "ok": True,
            "path": chunk_path,
            "heading": heading or heading_fb or "",
            "start_line": int(chunk.get("start_line") or 1),
            "heading_level": int(chunk.get("heading_level") or 0),
            "from_hash": True,
        }

    # Stale hash: retry via path + heading from the original search hit.
    if want and heading_fb:
        return {
            "ok": True,
            "path": want,
            "heading": heading_fb,
            "start_line": None,
            "heading_level": None,
            "from_hash": False,
            "tip": STALE_HASH_TIP,
        }

    msg = f"chunk_hash {ch!r} not found"
    if want and not heading_fb:
        msg += " (pass heading= from the search hit to fall back)"
    elif heading_fb and not want:
        msg += " (pass path= from the search hit to fall back)"
    return {
        "ok": False,
        "error": "anchor_not_found",
        "message": msg,
        **({"path": want} if want else {}),
    }


def materialize_ops_chunk_hashes(
    binding: vaults.VaultBinding,
    path: str,
    ops: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace ``chunk_hash`` on patch ops with resolved ``heading`` / ``scope.heading``.

    Returns ``{ok, ops, tips}`` or ``{ok: False, error, message, …}``.
    """
    tips: list[str] = []
    out: list[dict[str, Any]] = []
    note_path = path.replace("\\", "/").strip()

    for i, raw in enumerate(ops):
        op = dict(raw)
        kind = op.get("op")
        hashes: list[str] = []
        top_ch = op.pop("chunk_hash", None)
        if isinstance(top_ch, str) and top_ch.strip():
            hashes.append(top_ch.strip())

        if kind == "replace_text" and isinstance(op.get("scope"), dict):
            scope_raw = dict(op["scope"])
            scope_ch = scope_raw.pop("chunk_hash", None)
            if isinstance(scope_ch, str) and scope_ch.strip():
                hashes.append(scope_ch.strip())
            if scope_raw.get("heading") is not None:
                op["scope"] = {"heading": scope_raw["heading"]}
            elif scope_raw:
                op["scope"] = scope_raw
            else:
                op.pop("scope", None)

        if len(set(hashes)) > 1:
            return {
                "ok": False,
                "error": "bad_request",
                "message": (
                    f"op[{i}] conflicting chunk_hash values: "
                    f"{hashes[0]!r} vs {hashes[1]!r}"
                ),
                "path": note_path,
                "op_index": i,
            }
        ch = hashes[0] if hashes else None

        if not ch:
            out.append(op)
            continue

        if kind not in ("append", "prepend", "replace_section", "replace_text"):
            return {
                "ok": False,
                "error": "bad_request",
                "message": (
                    f"op[{i}] {kind!r} does not accept chunk_hash "
                    "(use append/prepend/replace_section/replace_text)"
                ),
                "path": note_path,
                "op_index": i,
            }

        heading_fb: str | None = None
        if kind == "replace_text":
            scope = op.get("scope") if isinstance(op.get("scope"), dict) else {}
            heading_fb = scope.get("heading") if isinstance(scope, dict) else None
            if heading_fb is None:
                heading_fb = op.get("heading")
        else:
            heading_fb = op.get("heading")

        resolved = resolve_chunk_anchor(
            binding,
            ch,
            path=note_path,
            heading_fallback=str(heading_fb) if heading_fb else None,
        )
        if not resolved.get("ok"):
            resolved = {**resolved, "path": note_path, "op_index": i}
            return resolved

        if resolved.get("path") and resolved["path"] != note_path:
            return {
                "ok": False,
                "error": "path_mismatch",
                "message": (
                    f"op[{i}] chunk_hash belongs to {resolved['path']!r}, "
                    f"not {note_path!r}"
                ),
                "path": note_path,
                "op_index": i,
            }

        heading = (resolved.get("heading") or "").strip()
        if not heading:
            return {
                "ok": False,
                "error": "anchor_not_found",
                "message": f"op[{i}] chunk_hash resolved without a heading",
                "path": note_path,
                "op_index": i,
            }

        tip = resolved.get("tip")
        if tip:
            tips.append(tip)

        if kind == "replace_text":
            op["scope"] = {"heading": heading}
            op.pop("heading", None)
        else:
            op["heading"] = heading

        out.append(op)

    return {"ok": True, "ops": out, "tips": tips}
