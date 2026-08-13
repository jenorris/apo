"""Obsidian-OFM → GFM unwrap for on-the-fly HTML render (no disk sidecars).

Ported from Workbench ``harness/export/scripts/ofm_unwrap.py`` — preview path only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
COMMENT = re.compile(r"%%.*?%%", re.S)
HIGHLIGHT = re.compile(r"==([^\n]+?)==")
WIKILINK_PIPE = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
CALLOUT_START = re.compile(r"^(>\s*)\[!([A-Za-z0-9_-]+)\]([+-]?)\s*(.*)$")

VALID_LAYOUTS = ("policy", "memo", "newspaper", "ops-card")

_SCALAR_FIELD = re.compile(
    r"(?m)^(title|policy_id|status|timestamp|confluence_last_modified|"
    r"confluence_version|confluence_page_id|resource|classification|"
    r"htmlize_layout|okf_type):\s*['\"]?(.+?)['\"]?\s*$"
)


def parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm = m.group(1)
    meta: dict[str, str] = {}
    for fm_match in _SCALAR_FIELD.finditer(fm):
        meta[fm_match.group(1)] = fm_match.group(2).strip()
    return meta


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1)


def unwrap_callouts(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        m = CALLOUT_START.match(line)
        if not m:
            out.append(line)
            continue
        prefix, ctype, _fold, rest = m.groups()
        rest = rest.strip()
        marker = f'<span class="ofm ofm-{ctype.lower()}" hidden></span>'
        out.append(f"{prefix}{marker}**{rest}**" if rest else f"{prefix}{marker}")
    return "\n".join(out)


def drop_leading_h1(body: str) -> tuple[str | None, str]:
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines = lines[1:]
    h1: str | None = None
    if lines and lines[0].startswith("# "):
        h1 = lines[0][2:].strip()
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return h1, "\n".join(lines) + ("\n" if lines else "")


def resolve_layout(
    path: Path,
    meta: dict[str, str],
    cli_layout: str | None,
    *,
    default_layout: str = "memo",
) -> str:
    if cli_layout and cli_layout in VALID_LAYOUTS:
        return cli_layout
    if meta.get("htmlize_layout") in VALID_LAYOUTS:
        return meta["htmlize_layout"]
    if "/policies/" in path.as_posix() or meta.get("okf_type", "").lower() == "policy":
        return "policy"
    if default_layout in VALID_LAYOUTS:
        return default_layout
    return "memo"


def _wikilink_target(raw: str) -> tuple[str, str]:
    """Return (display_text, vault-relative path hint)."""
    target = raw.strip()
    display = target
    if "|" in target:
        # already split by pipe regex usually; keep defensive
        left, right = target.split("|", 1)
        target, display = left.strip(), right.strip() or left.strip()
    # Strip heading anchors for path resolution
    path_part = target.split("#", 1)[0].strip()
    if path_part and not path_part.endswith(".md"):
        path_part = path_part + ".md"
    return display or path_part, path_part


def inline_ofm(
    text: str,
    *,
    resolve_wikilinks: str = "plain",
    path_allowed: Any | None = None,
) -> str:
    """Normalize OFM inline syntax. Optionally rewrite wikilinks to /note routes."""
    text = COMMENT.sub("", text)
    text = HIGHLIGHT.sub(r"**\1**", text)

    def _pipe(m: re.Match[str]) -> str:
        target, display = m.group(1).strip(), m.group(2).strip()
        return _format_wikilink(target, display or target, resolve_wikilinks, path_allowed)

    def _bare(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        display, path_hint = _wikilink_target(raw)
        return _format_wikilink(path_hint.replace(".md", ""), display, resolve_wikilinks, path_allowed, path_hint)

    text = WIKILINK_PIPE.sub(_pipe, text)
    text = WIKILINK.sub(_bare, text)
    return text


def _format_wikilink(
    target: str,
    display: str,
    mode: str,
    path_allowed: Any | None,
    path_hint: str | None = None,
) -> str:
    display = display.strip() or target.strip()
    if mode != "serve-routes":
        return display
    hint = path_hint
    if hint is None:
        hint = target.strip()
        if hint and not hint.endswith(".md"):
            hint = hint + ".md"
    hint = (hint or "").replace("\\", "/").lstrip("/")
    allowed = True
    if path_allowed is not None and hint:
        try:
            allowed = bool(path_allowed(hint))
        except Exception:
            allowed = False
    if allowed and hint:
        return f"[{display}](/note?path={quote(hint)})"
    return display


def build_gfm_body(
    raw: str,
    path: Path,
    *,
    cli_layout: str | None = None,
    default_layout: str = "memo",
    strip_fm: bool = True,
    resolve_wikilinks: str = "plain",
    path_allowed: Any | None = None,
) -> tuple[dict[str, str], str, str]:
    """Return (meta, layout, gfm_body)."""
    meta = parse_frontmatter(raw)
    layout = resolve_layout(path, meta, cli_layout, default_layout=default_layout)
    body = strip_frontmatter(raw) if strip_fm else raw
    body = inline_ofm(
        body,
        resolve_wikilinks=resolve_wikilinks,
        path_allowed=path_allowed,
    )
    body = unwrap_callouts(body)
    h1, body = drop_leading_h1(body)
    if h1 and "title" not in meta:
        meta["title"] = h1
    if "title" not in meta:
        meta["title"] = path.stem
    return meta, layout, body


def pandoc_metadata(meta: dict[str, str], layout: str) -> dict[str, str]:
    def _subtitle() -> str:
        bits = [meta.get("policy_id"), meta.get("status")]
        return " · ".join(b for b in bits if b)

    def _date() -> str:
        return (
            meta.get("confluence_last_modified")
            or (meta.get("timestamp") or "")[:10]
        )

    def _footer() -> str:
        bits = [meta.get("title"), meta.get("policy_id"), _date()]
        return "  ·  ".join(b for b in bits if b)

    out = {
        "title": meta.get("title") or "",
        "subtitle": _subtitle(),
        "date": _date(),
        "toc-title": "Contents",
        "htmlize_layout": layout,
        "print_footer": _footer(),
    }
    return {k: v for k, v in out.items() if v}
