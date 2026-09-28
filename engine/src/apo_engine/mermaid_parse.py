"""Mermaid diagram parsing — nodes, edges, subgraphs for indexing.

Pure Python parser for common flowchart and sequenceDiagram dialects.
Optional Node subprocess when ``APO_MERMAID_PARSER`` or vendored script is set.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Node "shape" alternatives shared by standalone declarations and inline
# edge endpoints — order matters (more specific delimiters first) so that
# e.g. `[[Label]]` (subroutine) isn't swallowed by the plain `[Label]` (rect)
# alternative one char short.
_SHAPE_ALT = (
    r"\[\[([^\]]+)\]\]|"   # subroutine [[label]]
    r"\(\[([^\]]+)\]\)|"   # stadium ([label])
    r"\(\(([^)]+)\)\)|"    # circle ((label))
    r"\[([^\]]+)\]|"       # rect [label]
    r"\(([^)]+)\)|"        # round (label)
    r"\{([^}]+)\}|"        # diamond {label}
    r">([^>]+)>"           # asymmetric >label>
)

# Edge endpoint: id + optional shape, consumed greedily but not anchored to
# end-of-string (callers check match.end() against the segment length to
# confirm the whole segment was consumed).
_NODE_TOKEN_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(?:" + _SHAPE_ALT + r")?\s*")

# Standalone node declaration: `A["Label"]` / `A[Label]` / `A` alone on a line.
_STANDALONE_NODE_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(?:" + _SHAPE_ALT + r")?\s*$")

# Arrow operators, longest/most-specific first so an arrowhead variant isn't
# truncated to its bare-line fallback (e.g. `-->` must not stop at `--`).
_ARROW_RE = re.compile(
    r"(-\.->|-\.-|=+>|--+>|=+|--+)(?:\s*\|\s*([^|]+?)\s*\|)?"
)

_SUBGRAPH_RE = re.compile(
    r'^\s*subgraph\s+(\w+)(?:\s*\[\s*"([^"]+)"\s*\]|\s*\[\s*([^\]]+?)\s*\])?\s*$',
    re.I,
)
_END_SUBGRAPH_RE = re.compile(r"^\s*end\s*$", re.I)

# Diagram types with no flowchart/sequence-shaped body — index at file level
# only (title/type via header/file chunks), never run node/edge regexes on
# their data lines (bar values, commit ids, gantt bars, ... would otherwise
# get parsed as garbage nodes).
_FILE_ONLY_DIAGRAM_TYPES = frozenset(
    {
        "xychart-beta",
        "gitgraph",
        "pie",
        "classdiagram",
        "statediagram-v2",
        "statediagram",
        "erdiagram",
        "journey",
        "gantt",
        "mindmap",
        "timeline",
        "quadrantchart",
        "requirementdiagram",
        "c4context",
        "sankey-beta",
        "block-beta",
        "kanban",
        "packet-beta",
        "architecture-beta",
    }
)

_DIAGRAM_TYPE_RE = re.compile(
    r"^\s*(flowchart|graph|sequenceDiagram|xychart-beta|gitGraph|pie|"
    r"classDiagram|stateDiagram-v2|stateDiagram|erDiagram|journey|gantt|"
    r"mindmap|timeline|quadrantChart|requirementDiagram|C4Context|"
    r"sankey-beta|block-beta|kanban|packet-beta|architecture-beta)"
    r"(?:\s+(\S+))?\s*$",
    re.I,
)
_PARTICIPANT_RE = re.compile(
    r"^\s*participant\s+(\S+)(?:\s+as\s+(.+))?\s*$", re.I
)
# Sequence-diagram arrow: id charset excludes `-`/`=`/`>` so e.g. `API-->>MCP`
# yields participant `API`, not `API-` (the arrow is never partially
# swallowed by a greedy `\S+` id match).
_SEQ_ARROW_RE = re.compile(
    r"^\s*([A-Za-z0-9_.]+)\s*(-+>>|--+>|->>|->)\s*([A-Za-z0-9_.]+)\s*(?::\s*(.+))?\s*$"
)
_COMMENT_RE = re.compile(r"^\s*%%")


@dataclass
class MermaidNode:
    node_id: str
    label: str = ""
    subgraph: str = ""


@dataclass
class MermaidEdge:
    from_id: str
    to_id: str
    label: str = ""


@dataclass
class MermaidDiagram:
    diagram_type: str = ""
    direction: str = ""
    nodes: list[MermaidNode] = field(default_factory=list)
    edges: list[MermaidEdge] = field(default_factory=list)
    subgraphs: list[str] = field(default_factory=list)
    parse_error: str = ""
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.parse_error


def _pick_label(groups: tuple) -> str:
    for g in groups:
        if g and str(g).strip():
            return str(g).strip().strip('"')
    return ""


def _node_label_from_match(node_id: str, groups: tuple) -> str:
    lbl = _pick_label(groups)
    return lbl or node_id


_SHAPE_OPENERS = {"[": "]", "(": ")", "{": "}"}


def _mask_shapes(line: str) -> str:
    """Blank out bracket/paren/brace/quote interiors with a neutral char.

    Node labels routinely contain characters that also spell arrow operators
    out of context — `EXPLAIN FORMAT=JSON`, `>= row threshold`, `X-infra-vY`.
    Arrow scanning must skip label interiors so it never mistakes a stray
    `-`/`=` inside a label for an edge. Delimiters themselves are left in
    place (unmasked) so the result stays the same length and callers can
    still slice segments — including shape delimiters — from the original
    line using match spans found against this masked copy.
    """
    out = list(line)
    stack: list[str] = []
    in_quote = False
    for i, ch in enumerate(line):
        if in_quote:
            if ch == '"':
                in_quote = False
            else:
                out[i] = "x"
            continue
        if stack:
            closer = _SHAPE_OPENERS[stack[-1]]
            if ch == closer:
                stack.pop()
            elif ch in _SHAPE_OPENERS:
                stack.append(ch)
            else:
                out[i] = "x"
            continue
        if ch == '"':
            in_quote = True
        elif ch in _SHAPE_OPENERS:
            stack.append(ch)
    return "".join(out)


def _split_arrow_chain(line: str) -> tuple[list[str], list[str]] | None:
    """Split a (possibly chained) edge line at each arrow.

    Returns ``(segments, labels)`` where ``segments`` has one more element
    than ``labels`` (N nodes, N-1 arrows), or ``None`` if the line has no
    arrow at all. Arrows are located on a masked copy of the line (label/
    shape interiors blanked, see ``_mask_shapes``) so an arrow-like
    substring inside a node label is never mistaken for an edge; segments
    and labels are then sliced back out of the original line.
    """
    scan = _mask_shapes(line)
    matches = list(_ARROW_RE.finditer(scan))
    if not matches:
        return None
    segments: list[str] = []
    labels: list[str] = []
    pos = 0
    for m in matches:
        segments.append(line[pos : m.start()])
        if m.group(2) is not None:
            lstart, lend = m.span(2)
            labels.append(line[lstart:lend].strip().strip('"'))
        else:
            labels.append("")
        pos = m.end()
    segments.append(line[pos:])
    return segments, labels


def parse_mermaid(text: str) -> MermaidDiagram:
    """Parse Mermaid source into nodes and edges."""
    text = (text or "").strip()
    if not text:
        return MermaidDiagram(parse_error="empty diagram")

    parsed = _parse_python(text)

    env_parser = os.environ.get("APO_MERMAID_PARSER", "").strip()
    if env_parser:
        sub = _parse_via_subprocess(text, env_parser)
        if sub is not None and sub.ok and len(sub.nodes) >= len(parsed.nodes):
            return sub

    if os.environ.get("APO_MERMAID_USE_VENDORED", "").strip() in ("1", "true", "yes"):
        script = Path(__file__).resolve().parents[2] / "scripts" / "mermaid_parse.mjs"
        if script.is_file():
            sub = _parse_via_subprocess(text, str(script))
            if sub is not None and sub.ok and len(sub.nodes) >= len(parsed.nodes):
                return sub

    return parsed


def _parse_via_subprocess(text: str, command: str) -> MermaidDiagram | None:
    try:
        proc = subprocess.run(
            ["node", command],
            input=text,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return _diagram_from_dict(data)


def _diagram_from_dict(data: dict[str, Any]) -> MermaidDiagram:
    diag = MermaidDiagram(
        diagram_type=str(data.get("diagram_type") or "flowchart"),
        direction=str(data.get("direction") or ""),
    )
    if data.get("error"):
        diag.parse_error = str(data["error"])
        return diag
    for sg in data.get("subgraphs") or []:
        if isinstance(sg, str) and sg.strip():
            diag.subgraphs.append(sg.strip())
    seen: set[str] = set()
    for raw in data.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        nid = str(raw.get("id") or "").strip()
        if not nid or nid in seen:
            continue
        seen.add(nid)
        diag.nodes.append(
            MermaidNode(
                node_id=nid,
                label=str(raw.get("label") or nid).strip(),
                subgraph=str(raw.get("subgraph") or "").strip(),
            )
        )
    for raw in data.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        fr = str(raw.get("from") or raw.get("from_id") or "").strip()
        to = str(raw.get("to") or raw.get("to_id") or "").strip()
        if fr and to:
            diag.edges.append(
                MermaidEdge(fr, to, str(raw.get("label") or "").strip())
            )
    return diag


def _register_node(
    node_map: dict[str, MermaidNode], node_id: str, label: str, subgraph: str
) -> None:
    existing = node_map.get(node_id)
    if existing is None:
        node_map[node_id] = MermaidNode(node_id=node_id, label=label or node_id, subgraph=subgraph)
        return
    # Don't let a bare edge-endpoint reference (label falls back to the id)
    # clobber a real label captured from a shaped declaration elsewhere.
    if label and label != node_id and (not existing.label or existing.label == existing.node_id):
        existing.label = label
    if subgraph and not existing.subgraph:
        existing.subgraph = subgraph


def _parse_python(text: str) -> MermaidDiagram:
    lines = text.splitlines()
    diag = MermaidDiagram()
    subgraph_stack: list[str] = []
    node_map: dict[str, MermaidNode] = {}
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line.strip() or _COMMENT_RE.match(line):
            continue
        if diag.diagram_type in _FILE_ONLY_DIAGRAM_TYPES:
            # Non-flow diagram type already detected — no per-node/per-edge
            # parsing for these dialects, just consume the remaining lines.
            continue
        mtype = _DIAGRAM_TYPE_RE.match(line)
        if mtype and not diag.diagram_type:
            diag.diagram_type = mtype.group(1).lower()
            if mtype.group(2):
                diag.direction = mtype.group(2).upper()
            continue
        if diag.diagram_type == "sequencediagram":
            pm = _PARTICIPANT_RE.match(line)
            if pm:
                nid = pm.group(1).strip()
                lbl = (pm.group(2) or nid).strip().strip('"')
                node_map[nid] = MermaidNode(node_id=nid, label=lbl)
                continue
            sm = _SEQ_ARROW_RE.match(line)
            if sm:
                fr, to = sm.group(1).strip(), sm.group(3).strip()
                elbl = (sm.group(4) or "").strip()
                for nid in (fr, to):
                    if nid not in node_map:
                        node_map[nid] = MermaidNode(node_id=nid, label=nid)
                diag.edges.append(MermaidEdge(fr, to, elbl))
            continue
        sg = _SUBGRAPH_RE.match(line)
        if sg:
            sg_id = sg.group(1).strip()
            sg_label = (sg.group(2) or sg.group(3) or sg_id).strip()
            subgraph_stack.append(sg_label)
            if sg_label not in diag.subgraphs:
                diag.subgraphs.append(sg_label)
            continue
        if _END_SUBGRAPH_RE.match(line):
            if subgraph_stack:
                subgraph_stack.pop()
            continue
        current_sg = subgraph_stack[-1] if subgraph_stack else ""

        chain = _split_arrow_chain(line)
        if chain is not None:
            segments, labels = chain
            chain_nodes: list[tuple[str, str]] = []
            chain_ok = True
            for seg in segments:
                nm = _NODE_TOKEN_RE.match(seg)
                if not nm or nm.end() != len(seg):
                    chain_ok = False
                    break
                nid = nm.group(1)
                lbl = _node_label_from_match(nid, nm.groups()[1:])
                chain_nodes.append((nid, lbl))
            if chain_ok and len(chain_nodes) >= 2:
                for nid, lbl in chain_nodes:
                    _register_node(node_map, nid, lbl, current_sg)
                for idx in range(len(chain_nodes) - 1):
                    diag.edges.append(
                        MermaidEdge(chain_nodes[idx][0], chain_nodes[idx + 1][0], labels[idx])
                    )
                continue
            # Line looked like it had an arrow but didn't parse cleanly
            # (unrecognized shape/id syntax) — fall through silently rather
            # than guess, matching the parser's existing conservative stance.
            continue

        # Standalone node declaration: A["Label"] or A[Label]
        nm = _STANDALONE_NODE_RE.match(line)
        if nm:
            nid = nm.group(1)
            lbl = _node_label_from_match(nid, nm.groups()[1:])
            node_map[nid] = MermaidNode(node_id=nid, label=lbl, subgraph=current_sg)
            continue

    diag.nodes = list(node_map.values())
    if not diag.diagram_type and text.strip():
        diag.diagram_type = "flowchart"
    file_only = diag.diagram_type in _FILE_ONLY_DIAGRAM_TYPES
    if not file_only:
        if not diag.nodes and not diag.edges and text.strip():
            diag.parse_error = "no nodes or edges parsed"
        elif text.strip() and not diag.edges:
            if re.search(r"-->\s*$", text, re.M) or re.search(r"-->\s*\n\s*$", text):
                diag.parse_error = "incomplete edge(s)"
    return diag
