"""Mermaid chunk indexing — flattened embed rows (table analog)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import mermaid_contract as mc
from . import mermaid_parse as mp
from . import vaults

_FM_FENCE_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*\n", re.M)


def diagram_id_for(rel: str, block_index: int | None = None) -> str:
    if block_index is not None:
        return f"{rel}#mermaid-{block_index}"
    return rel


def merge_frontmatter_for_mmd(
    text: str,
    rel: str,
    mtime: float | None = None,
) -> dict[str, Any]:
    """Build files.frontmatter for a .mmd note (catalog + optional YAML FM)."""
    root = vaults.notes_root()
    fm: dict[str, Any] = dict(mc.catalog_entry_for(root, rel))
    fence = _FM_FENCE_RE.match(text or "")
    if fence:
        import yaml

        try:
            override = yaml.safe_load(fence.group(1)) or {}
        except yaml.YAMLError:
            override = {}
        if isinstance(override, dict):
            fm.update(override)
    if not fm.get("diagram_id"):
        path = Path(rel.replace("\\", "/"))
        if path.name == "diagram.mmd" and path.parent.name:
            fm["diagram_id"] = path.parent.name
        else:
            fm["diagram_id"] = path.stem
    fm.setdefault("okf_type", "Diagram")
    fm.setdefault("title", fm.get("title") or str(fm.get("diagram_id", "")).replace("-", " ").title())
    fm.setdefault("description", fm.get("title"))
    fm.setdefault("type", fm.get("type") or "flowchart")
    if mtime is not None and not fm.get("timestamp"):
        fm["timestamp"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return fm


def node_flatten_text(
    title: str,
    subgraph: str,
    node_id: str,
    label: str,
    *,
    template: str = "",
) -> str:
    tpl = template or "{title} > {subgraph} > {node} — {label}"
    parts = [p for p in (title, subgraph) if p]
    prefix = " > ".join(parts)
    node_part = f"{node_id} — {label}" if label and label != node_id else node_id
    if "{title}" in tpl:
        return tpl.format(
            title=title,
            subgraph=subgraph or "",
            node=node_id,
            label=label or node_id,
        ).replace(" >  > ", " > ").strip(" >")
    if prefix:
        return f"{prefix} > {node_part}"
    return node_part


def edge_flatten_text(title: str, edge: mp.MermaidEdge) -> str:
    base = f"{title} > {edge.from_id} --> {edge.to_id}" if title else f"{edge.from_id} --> {edge.to_id}"
    if edge.label:
        return f"{base} — {edge.label}"
    return base


def header_flatten_text(title: str, diagram: mp.MermaidDiagram) -> str:
    if diagram.subgraphs:
        names = ", ".join(diagram.subgraphs)
        return f"{title} > subgraphs: {names}" if title else f"subgraphs: {names}"
    dtype = diagram.diagram_type
    if diagram.direction:
        return f"{title} — {dtype} {diagram.direction}" if title else f"{dtype} {diagram.direction}"
    return f"{title} — {dtype}" if title else dtype


def file_flatten_text(title: str, diagram: mp.MermaidDiagram, *, top_n: int = 8) -> str:
    labels = []
    for n in diagram.nodes[:top_n]:
        lbl = n.label or n.node_id
        if lbl not in labels:
            labels.append(lbl)
    kw = ", ".join(labels)
    dtype = diagram.diagram_type
    if title and kw:
        return f"{title} · {dtype} · nodes: {kw}"
    if title:
        return f"{title} · {dtype}"
    return kw or dtype


def mermaid_block_id(rel: str, abs_start: int, block_index: int) -> str:
    raw = f"{rel}:{abs_start}:{block_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def catalog_search_prefix(cat: dict[str, Any]) -> str:
    """Slug/title/type tokens prepended to mermaid chunk embed text."""
    if not cat:
        return ""
    parts: list[str] = []
    slug = str(cat.get("diagram_id") or cat.get("slug") or "").strip()
    if slug:
        parts.append(slug)
        parts.extend(p for p in slug.split("-") if p)
    title = str(cat.get("title") or "").strip()
    if title:
        parts.append(title)
    dtype = str(cat.get("type") or "").strip()
    if dtype:
        parts.append(dtype)
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        key = part.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(part)
    return " ".join(out)


def _entity_search_tokens(node_id: str, label: str) -> str:
    """Extra recall tokens for entity-style queries (RAPI, Tuition, …)."""
    tokens: list[str] = []
    if node_id:
        tokens.append(node_id)
    label = (label or "").strip()
    if label:
        tokens.append(label)
        for word in re.findall(r"[A-Za-z]+", label):
            if len(word) >= 4 or (word.isupper() and len(word) >= 2):
                tokens.append(word)
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        key = tok.lower()
        if key not in seen:
            seen.add(key)
            out.append(tok)
    return " ".join(out)


def _with_search_prefix(prefix: str, text: str) -> str:
    if prefix and text:
        return f"{prefix} · {text}"
    return prefix or text


def chunk_mermaid_rows(
    text: str,
    rel: str,
    *,
    breadcrumb: list[str] | None = None,
    block_index: int | None = None,
    abs_start_line: int = 1,
    parent_chunk_hash: str = "",
    id_prefix: str = "",
    model_name: str = "",
    content_hash_fn=None,
    compute_chunk_id_fn=None,
    ord_start: int = 0,
) -> tuple[list[tuple], int]:
    """Emit pending chunk tuples for one mermaid source block.

    Returns (rows, next_ord). Each row matches _markdown_chunk_rows 11-tuple shape.
    """
    from . import config
    from .core import _content_hash, compute_chunk_id

    _hash = content_hash_fn or _content_hash
    _cid = compute_chunk_id_fn or compute_chunk_id
    _model = model_name or config.MODEL_NAME
    _prefix = id_prefix or f"markdown:{rel}"

    root = vaults.notes_root()
    strategy = mc.chunk_strategy_for(root, rel)
    include_edges = mc.include_edge_chunks(root, rel)
    template = mc.flatten_template_for(root, rel)

    crumbs = list(breadcrumb or [])
    title = crumbs[0] if crumbs else Path(rel).stem.replace("-", " ").title()
    cat: dict[str, Any] = {}
    if block_index is not None:
        did = diagram_id_for(rel, block_index)
    else:
        cat = mc.catalog_entry_for(root, rel)
        title = str(cat.get("title") or title)
        did = str(cat.get("diagram_id") or diagram_id_for(rel))

    catalog_prefix = catalog_search_prefix(cat) if block_index is None else ""
    diagram = mp.parse_mermaid(text)
    rows: list[tuple] = []
    ord_counter = ord_start
    level = 0
    parent_bc = " › ".join(crumbs[1:]) if len(crumbs) > 1 else ""

    block_id = mermaid_block_id(rel, abs_start_line, block_index or 0)

    def _append(
        flat: str,
        line: int,
        kind: str,
        *,
        row_key: str = "",
        node_index: int | None = None,
    ) -> None:
        nonlocal ord_counter
        chash = _hash(flat)
        cid = _cid(_prefix, line, line, chash, _model)
        meta = {
            "chunk_kind": kind,
            "parent_chunk_hash": parent_chunk_hash,
            "table_id": did,
            "table_schema_hash": block_id,
            "row_index": node_index,
            "row_key": row_key or did,
        }
        rows.append(
            (
                rel,
                ord_counter,
                parent_bc,
                flat,
                line,
                line,
                level,
                cid,
                chash,
                len(flat.encode("utf-8")),
                meta,
            )
        )
        ord_counter += 1

    if not diagram.ok:
        fallback = file_flatten_text(title, diagram) if title else text[:200]
        if not fallback.strip():
            fallback = rel
        _append(_with_search_prefix(catalog_prefix, fallback), abs_start_line, "mermaid_file")
        return rows, ord_counter

    _append(
        _with_search_prefix(catalog_prefix, file_flatten_text(title, diagram)),
        abs_start_line,
        "mermaid_file",
    )

    if strategy != "file_only":
        _append(
            _with_search_prefix(catalog_prefix, header_flatten_text(title, diagram)),
            abs_start_line,
            "mermaid_header",
        )
        for ni, node in enumerate(diagram.nodes):
            flat = node_flatten_text(
                title,
                node.subgraph,
                node.node_id,
                node.label or node.node_id,
                template=template,
            )
            entity = _entity_search_tokens(node.node_id, node.label or node.node_id)
            if entity:
                flat = f"{flat} · {entity}"
            _append(
                flat,
                abs_start_line,
                "mermaid_node",
                row_key=node.node_id,
                node_index=ni,
            )

    if strategy == "nodes_and_edges" and include_edges:
        for edge in diagram.edges:
            _append(
                edge_flatten_text(title, edge),
                abs_start_line,
                "mermaid_edge",
                row_key=f"{edge.from_id}->{edge.to_id}",
            )

    return rows, ord_counter
