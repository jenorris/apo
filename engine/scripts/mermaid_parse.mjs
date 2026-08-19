#!/usr/bin/env node
/** Minimal Mermaid parse wrapper — reads stdin, emits JSON AST on stdout. */
import { readFileSync } from "fs";

const input = readFileSync(0, "utf8");
const lines = input.split(/\r?\n/);
const out = { diagram_type: "flowchart", direction: "", nodes: [], edges: [], subgraphs: [] };
let subgraph = "";
for (const raw of lines) {
  const line = raw.trim();
  if (!line || line.startsWith("%%")) continue;
  const head = line.match(/^(flowchart|graph|sequenceDiagram)\s+(\S+)?/i);
  if (head) {
    out.diagram_type = head[1].toLowerCase();
    out.direction = head[2] || "";
    continue;
  }
  const sg = line.match(/^subgraph\s+(\w+)(?:\s*\["([^"]+)"\])?/i);
  if (sg) {
    subgraph = sg[2] || sg[1];
    if (!out.subgraphs.includes(subgraph)) out.subgraphs.push(subgraph);
    continue;
  }
  if (/^end$/i.test(line)) {
    subgraph = "";
    continue;
  }
  const edge = line.match(/^(\w+)\s*(--+>|===+|---|--)\s*(?:\|\s*([^|]+)\s*\|\s*)?(\w+)/);
  if (edge) {
    out.edges.push({ from: edge[1], to: edge[4], label: (edge[3] || "").trim() });
    for (const id of [edge[1], edge[4]]) {
      if (!out.nodes.find((n) => n.id === id)) out.nodes.push({ id, label: id, subgraph });
    }
    continue;
  }
  const node = line.match(/^(\w+)(?:\["([^"]+)"\])?/);
  if (node) {
    const id = node[1];
    const label = node[2] || id;
    const existing = out.nodes.find((n) => n.id === id);
    if (existing) existing.label = label;
    else out.nodes.push({ id, label, subgraph });
  }
}
process.stdout.write(JSON.stringify(out));
