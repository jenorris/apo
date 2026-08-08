"""GFM pipe-table parsing, CSV/JSON round-trips, row flattening, fuzzy headers.

Pure functions only — no index, no I/O. The indexer turns each table into a
``table_header`` chunk plus one ``table_row`` chunk per data row; the patch
engine rewrites individual rows keyed by ``row_key``. Both share the parse and
serialize helpers here so a row read, a row hit, and a row write agree on cells.

Boundaries mirror :mod:`apo_engine.markdown_sections`: tables inside fenced code
blocks are ignored, and a table block is atomic (never split for chunking).
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_DELIM_CELL_RE = re.compile(r"^\s*:?-{1,}:?\s*$")
_WS_RE = re.compile(r"\s+")


@dataclass
class Table:
    """A parsed GFM table.

    ``start_line`` / ``end_line`` are 0-based inclusive indices into the line
    list passed to :func:`find_tables`. ``table_index`` is the 0-based ordinal of
    this table within the document (stable id input).
    """

    headers: list[str]
    rows: list[list[str]]
    start_line: int
    end_line: int
    table_index: int
    alignments: list[str] = field(default_factory=list)


def _split_row(line: str) -> list[str]:
    """Split a GFM table row into cells, honoring escaped pipes (``\\|``)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in s:
        if escaped:
            buf.append(ch if ch == "|" else "\\" + ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    cells.append("".join(buf).strip())
    return cells


def _is_delimiter_row(line: str) -> bool:
    s = line.strip()
    if "|" not in s and "-" not in s:
        return False
    cells = _split_row(line)
    if not cells or any(c == "" for c in cells):
        # A delimiter row must have a spec in every column.
        if not all(_DELIM_CELL_RE.match(c) for c in cells if c != ""):
            return False
    return bool(cells) and all(_DELIM_CELL_RE.match(c) for c in cells)


def _alignment(cell: str) -> str:
    c = cell.strip()
    left = c.startswith(":")
    right = c.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return ""


def find_tables(lines: list[str]) -> list[Table]:
    """Extract every GFM pipe table, skipping fenced code blocks."""
    tables: list[Table] = []
    fence: str | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            token = stripped[:3]
            fence = None if fence == token else (fence or token)
            i += 1
            continue
        if fence is not None:
            i += 1
            continue
        # A table needs a header line, a delimiter line, then >=0 data rows.
        if "|" in line and i + 1 < n and _is_delimiter_row(lines[i + 1]):
            header = _split_row(line)
            aligns = [_alignment(c) for c in _split_row(lines[i + 1])]
            start = i
            j = i + 2
            rows: list[list[str]] = []
            while j < n and "|" in lines[j] and lines[j].strip():
                cells = _split_row(lines[j])
                # Normalize ragged rows to header width.
                if len(cells) < len(header):
                    cells += [""] * (len(header) - len(cells))
                elif len(cells) > len(header):
                    cells = cells[: len(header)]
                rows.append(cells)
                j += 1
            tables.append(
                Table(
                    headers=header,
                    rows=rows,
                    start_line=start,
                    end_line=j - 1,
                    table_index=len(tables),
                    alignments=aligns,
                )
            )
            i = j
            continue
        i += 1
    return tables


def normalize_header(name: str) -> str:
    """Case/space/punct-insensitive header key for matching."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def table_schema_hash(headers: list[str]) -> str:
    """Hash of column names + order — invalidates on column rename/add/delete."""
    raw = "\u0000".join(normalize_header(h) for h in headers)
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=8).hexdigest()


def row_raw_hash(cells: list[str]) -> str:
    """Breadcrumb-independent hash of a row's raw cell values (write precondition).

    Mirrors ``core._content_hash`` (blake2b-64) so ``expected_row_hash`` from
    ``read_note(format=row)`` is comparable regardless of the note's heading trail.
    """
    raw = "\u0000".join(c.strip() for c in cells)
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=8).hexdigest()


def table_id_for(path: str, start_line: int, table_index: int) -> str:
    """Stable id for a table within a note (path + first line + ordinal)."""
    raw = f"{path}:{start_line}:{table_index}"
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=8).hexdigest()


def row_key_for(
    table: Table,
    row_index: int,
    *,
    key_column: str | None = None,
) -> str:
    """Natural key for a data row.

    Uses ``key_column`` (by normalized header) when given and non-empty; else the
    first non-empty cell; else a synthetic stable ``row-<n>``. Kept stable across
    reindex by falling back to the row ordinal only when no cell value exists.
    """
    if key_column:
        target = normalize_header(key_column)
        for h, cell in zip(table.headers, table.rows[row_index]):
            if normalize_header(h) == target and cell.strip():
                return cell.strip()
    row = table.rows[row_index]
    for cell in row:
        if cell.strip():
            return cell.strip()
    return f"row-{row_index}"


def table_to_records(table: Table) -> list[dict[str, str]]:
    return [dict(zip(table.headers, row)) for row in table.rows]


def row_flatten_text(breadcrumb: list[str], headers: list[str], row: list[str]) -> str:
    """Canonical one-line embed/FTS string for a data row.

    ``Pacifica > Maintenance History > Brake Work — Date: 2026-06-07, Mileage: 114587``

    Uses ``>`` (not the index metadata ``›``) for natural-language alignment with
    queries. Column labels are always kept — bare numerics embed weakly.
    """
    prefix = " > ".join(b for b in breadcrumb if b)
    pairs = []
    for h, cell in zip(headers, row):
        h = h.strip()
        cell = cell.strip()
        if not h and not cell:
            continue
        pairs.append(f"{h}: {cell}" if h else cell)
    body = ", ".join(pairs)
    if prefix and body:
        return f"{prefix} — {body}"
    return prefix or body


def header_flatten_text(breadcrumb: list[str], headers: list[str]) -> str:
    """Small ``table_header`` embed string — schema recall ('what columns …')."""
    prefix = " > ".join(b for b in breadcrumb if b)
    cols = ", ".join(h.strip() for h in headers if h.strip())
    if prefix:
        return f"{prefix} — Columns: {cols}"
    return f"Columns: {cols}"


# --------------------------------------------------------------------------- #
# Serialize
# --------------------------------------------------------------------------- #
def _escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def records_to_gfm(
    headers: list[str],
    rows: list[list[str]],
    *,
    alignments: list[str] | None = None,
) -> str:
    """Render headers + rows as a GFM pipe table (no surrounding blank lines)."""
    def _delim(i: int) -> str:
        a = (alignments or [])[i] if alignments and i < len(alignments) else ""
        if a == "center":
            return ":---:"
        if a == "right":
            return "---:"
        if a == "left":
            return ":---"
        return "---"

    head = "| " + " | ".join(_escape_cell(h) for h in headers) + " |"
    delim = "| " + " | ".join(_delim(i) for i in range(len(headers))) + " |"
    body_lines = []
    for row in rows:
        cells = list(row) + [""] * (len(headers) - len(row))
        cells = cells[: len(headers)]
        body_lines.append("| " + " | ".join(_escape_cell(c) for c in cells) + " |")
    return "\n".join([head, delim, *body_lines])


def dicts_to_rows(headers: list[str], records: list[dict[str, str]]) -> list[list[str]]:
    return [[str(rec.get(h, "")) for h in headers] for rec in records]


# --------------------------------------------------------------------------- #
# CSV / JSON
# --------------------------------------------------------------------------- #
def csv_to_records(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader]
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    records = []
    for raw in rows[1:]:
        if not any(c.strip() for c in raw):
            continue
        rec = {headers[i]: (raw[i] if i < len(raw) else "") for i in range(len(headers))}
        records.append(rec)
    return headers, records


def records_to_csv(headers: list[str], records: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    for rec in records:
        writer.writerow([rec.get(h, "") for h in headers])
    return buf.getvalue()


def csv_to_gfm(text: str) -> str:
    headers, records = csv_to_records(text)
    return records_to_gfm(headers, dicts_to_rows(headers, records))


def json_to_gfm(data: Any) -> str:
    """Accept ``[{col: val}, …]`` or ``{"headers": [...], "rows": [[...]]}``."""
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and "headers" in data:
        headers = [str(h) for h in data["headers"]]
        rows = [[str(c) for c in r] for r in data.get("rows", [])]
        return records_to_gfm(headers, rows)
    if isinstance(data, list):
        headers = []
        for rec in data:
            for k in rec:
                if k not in headers:
                    headers.append(k)
        rows = dicts_to_rows(headers, [{k: str(v) for k, v in rec.items()} for rec in data])
        return records_to_gfm(headers, rows)
    raise ValueError("json table must be a list of objects or {headers, rows}")


# --------------------------------------------------------------------------- #
# Fuzzy header mapping (day-one, ambiguity-reject)
# --------------------------------------------------------------------------- #
class HeaderAmbiguous(Exception):
    """Raised when incoming headers cannot be mapped confidently to existing ones."""

    def __init__(self, message: str, suggestions: list[dict]):
        super().__init__(message)
        self.suggestions = suggestions


def fuzzy_header_map(
    incoming: list[str],
    existing: list[str],
    *,
    cutoff: float = 0.72,
    allow_new_columns: bool = False,
) -> dict[str, str]:
    """Map each incoming header to an existing header by normalized fuzzy match.

    Never maps silently on a tie or low confidence — raises :class:`HeaderAmbiguous`
    with per-column suggestions so the caller can confirm or pass an explicit map.
    Exact normalized matches always win. Returns ``{incoming: existing}``.
    """
    norm_existing = {normalize_header(h): h for h in existing}
    existing_norms = list(norm_existing.keys())
    mapping: dict[str, str] = {}
    ambiguous: list[dict] = []

    for inc in incoming:
        ninc = normalize_header(inc)
        if ninc in norm_existing:
            mapping[inc] = norm_existing[ninc]
            continue
        scored = sorted(
            (
                (difflib.SequenceMatcher(None, ninc, ne).ratio(), ne)
                for ne in existing_norms
            ),
            reverse=True,
        )
        best = scored[0] if scored else (0.0, "")
        second = scored[1] if len(scored) > 1 else (0.0, "")
        if best[0] >= cutoff and (best[0] - second[0]) >= 0.08:
            mapping[inc] = norm_existing[best[1]]
        elif allow_new_columns:
            mapping[inc] = inc  # new column, kept as-is
        else:
            ambiguous.append(
                {
                    "incoming": inc,
                    "candidates": [
                        {"column": norm_existing[ne], "score": round(sc, 3)}
                        for sc, ne in scored[:3]
                        if ne
                    ],
                }
            )
    if ambiguous:
        cols = ", ".join(a["incoming"] for a in ambiguous)
        raise HeaderAmbiguous(
            f"header(s) {cols} could not be mapped confidently; pass an explicit "
            "column map or allow_new_columns=true",
            ambiguous,
        )
    return mapping
