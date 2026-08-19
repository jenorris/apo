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

_NODE_EDGE_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s*"
    r"(?:\[\[([^\]]+)\]\]|\[([^\]]+)\]|\(\(([^)]+)\)\)|\(([^)]+)\)|\{([^}]+)\}|>([^>]+)>|"
    r"(\[\([^\]]+\)\]))?"
    r"\s*(--+>|===+|---|--)\s*"
    r"(?:\|\s*([^|]+)\s*\|\s*)?"
    r"([A-Za-z0-9_]+)"
    r"(?:\[\[([^\]]+)\]\]|\[([^\]]+)\]|\(\(([^)]+)\)\)|\(([^)]+)\)|\{([^}]+)\}|>([^>]+)>|"
    r"(\[\([^\]]+\)\]))?"
    r"\s*(?:@\s*\{[^}]+\})?\s*$"
)
_SIMPLE_EDGE_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s*(--+>|===+|---|--)\s*"
    r"(?:\|\s*([^|]+)\s*\|\s*)?"
    r"([A-Za-z0-9_]+)\s*$"
)
_SUBGRAPH_RE = re.compile(
    r'^\s*subgraph\s+(\w+)(?:\s*\["([^"]+)"\])?\s*$', re.I
)
_END_SUBGRAPH_RE = re.compile(r"^\s*end\s*$", re.I)
_DIAGRAM_TYPE_RE = re.compile(
    r"^\s*(flowchart|graph|sequenceDiagram)(?:\s+(\S+))?\s*$", re.I
)
_PARTICIPANT_RE = re.compile(
    r"^\s*participant\s+(\S+)(?:\s+as\s+(.+))?\s*$", re.I
)
_SEQ_ARROW_RE = re.compile(
    r"^\s*(\S+)\s*(-+>>|--+>|->>|->)\s*(\S+)\s*(?::\s*(.+))?\s*$"
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
            sg_label = (sg.group(2) or sg_id).strip()
            subgraph_stack.append(sg_label)
            if sg_label not in diag.subgraphs:
                diag.subgraphs.append(sg_label)
            continue
        if _END_SUBGRAPH_RE.match(line):
            if subgraph_stack:
                subgraph_stack.pop()
            continue
        current_sg = subgraph_stack[-1] if subgraph_stack else ""
        sm = _SIMPLE_EDGE_RE.match(line)
        if sm:
            fr, _arrow, edge_lbl, to = sm.group(1), sm.group(2), sm.group(3), sm.group(4)
            edge_lbl = (edge_lbl or "").strip()
            for nid in (fr, to):
                if nid not in node_map:
                    node_map[nid] = MermaidNode(node_id=nid, label=nid, subgraph=current_sg)
                elif current_sg and not node_map[nid].subgraph:
                    node_map[nid].subgraph = current_sg
            diag.edges.append(MermaidEdge(fr, to, edge_lbl or ""))
            continue
        # Standalone node declaration: A["Label"] or A[Label]
        nm = re.match(
            r'^\s*([A-Za-z0-9_]+)\s*(?:\[\[([^\]]+)\]\]|\[([^\]]+)\]|\(\(([^)]+)\)\)|'
            r"\(([^)]+)\)|\{([^}]+)\}|>([^>]+)>)?\s*$",
            line,
        )
        if nm:
            nid = nm.group(1)
            lbl = _node_label_from_match(nid, nm.groups()[1:])
            node_map[nid] = MermaidNode(node_id=nid, label=lbl, subgraph=current_sg)
            continue

    diag.nodes = list(node_map.values())
    if not diag.diagram_type and text.strip():
        diag.diagram_type = "flowchart"
    if not diag.nodes and not diag.edges and text.strip():
        diag.parse_error = "no nodes or edges parsed"
    elif text.strip() and not diag.edges:
        if re.search(r"-->\s*$", text, re.M) or re.search(r"-->\s*\n\s*$", text):
            diag.parse_error = "incomplete edge(s)"
    return diag
