#!/usr/bin/env python3
"""Lint README / markdown share docs for objective breakage and editorial tells.

Exit codes:
  0 — no errors (warnings may still print)
  1 — one or more errors
  2 — usage / IO failure

By default, only objective breakage fails the process. Pass --strict to also
fail on warnings (slop tells, missing fence languages, badge density).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]+)\)")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HTML_IMG_RE = re.compile(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", re.I)
FENCE_RE = re.compile(r"^```(\w*)\s*$", re.MULTILINE)
BADGE_RE = re.compile(r"https?://img\.shields\.io/|https?://badgen\.net/", re.I)
PLACEHOLDER_RE = re.compile(
    r"\b(TODO|TBD|FIXME|lorem ipsum)\b|your-thing-here|xxx\b",
    re.I,
)
# Absolute-path install placeholders are intentional; do not flag them.
SLOP_RE = re.compile(
    r"\b("
    r"powerful|robust|seamless|cutting-edge|next-generation|next generation|"
    r"unlock|empower(?:s|ing)?|leverage|delightful|world-class|innovative|"
    r"game-changer|revolutionary"
    r")\b"
    r"|welcome to\b"
    r"|in this (?:guide|readme|document) we will\b"
    r"|whether you(?:'re| are) a\b",
    re.I,
)
EMOJI_HEADER_RE = re.compile(r"^#{1,6}\s+\W*[^\w\s#].+", re.MULTILINE)


@dataclass
class Finding:
    path: Path
    kind: str  # error | warning
    message: str
    line: int | None = None

    def format(self) -> str:
        loc = f"{self.path}:{self.line}: " if self.line else f"{self.path}: "
        return f"{self.kind.upper()}: {loc}{self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "warning"]


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _resolve_local(base: Path, target: str) -> Path | None:
    raw = target.strip()
    if not raw or raw.startswith(("#", "mailto:", "http://", "https://")):
        return None
    if raw.startswith("file:"):
        return None
    path_part = raw.split("#", 1)[0].split("?", 1)[0]
    if not path_part:
        return None
    return (base.parent / path_part).resolve()


def _headings(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for m in HEADING_RE.finditer(text):
        level = len(m.group(1))
        title = m.group(2).strip()
        out.append((_line_number(text, m.start()), level, title))
    return out


def lint_text(path: Path, text: str, report: Report) -> None:
    headings = _headings(text)
    if not headings and path.name.upper().startswith("README"):
        report.add(Finding(path, "error", "README has no headings", 1))

    seen_titles: dict[str, int] = {}
    prev_level: int | None = None
    for line, level, title in headings:
        key = title.casefold()
        if key in seen_titles:
            report.add(
                Finding(
                    path,
                    "error",
                    f"duplicate heading {title!r} (first at line {seen_titles[key]})",
                    line,
                )
            )
        else:
            seen_titles[key] = line

        if prev_level is not None and level > prev_level + 1:
            report.add(
                Finding(
                    path,
                    "error",
                    f"heading level jumps from H{prev_level} to H{level}",
                    line,
                )
            )
        prev_level = level

    heading_matches = list(HEADING_RE.finditer(text))
    for i, m in enumerate(heading_matches):
        body_start = m.end()
        body_end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(text)
        body = text[body_start:body_end].strip()
        if not body:
            report.add(
                Finding(
                    path,
                    "error",
                    f"empty section under {m.group(2).strip()!r}",
                    _line_number(text, m.start()),
                )
            )

    for m in PLACEHOLDER_RE.finditer(text):
        # Skip fenced code blocks for absolute-path style? Still flag TODO/TBD anywhere.
        report.add(
            Finding(
                path,
                "error",
                f"placeholder text {m.group(0)!r}",
                _line_number(text, m.start()),
            )
        )

    for m in LINK_RE.finditer(text):
        target = m.group(2).strip()
        local = _resolve_local(path, target)
        if local is None:
            continue
        if not local.exists():
            report.add(
                Finding(
                    path,
                    "error",
                    f"broken local link ({target})",
                    _line_number(text, m.start()),
                )
            )

    for m in IMAGE_RE.finditer(text):
        target = m.group(2).strip()
        local = _resolve_local(path, target)
        if local is None:
            continue
        if not local.exists():
            report.add(
                Finding(
                    path,
                    "error",
                    f"broken local image ({target})",
                    _line_number(text, m.start()),
                )
            )

    for m in HTML_IMG_RE.finditer(text):
        target = m.group(1).strip()
        local = _resolve_local(path, target)
        if local is None:
            continue
        if not local.exists():
            report.add(
                Finding(
                    path,
                    "error",
                    f"broken local <img> src ({target})",
                    _line_number(text, m.start()),
                )
            )

    # Fenced code: language tags
    in_fence = False
    for m in FENCE_RE.finditer(text):
        lang = m.group(1)
        line = _line_number(text, m.start())
        if not in_fence:
            in_fence = True
            if not lang:
                report.add(
                    Finding(
                        path,
                        "warning",
                        "code fence missing language tag",
                        line,
                    )
                )
        else:
            in_fence = False

    badge_hits = list(BADGE_RE.finditer(text))
    if len(badge_hits) > 4:
        report.add(
            Finding(
                path,
                "warning",
                f"excessive badges ({len(badge_hits)} shields/badgen URLs; prefer ≤4)",
                _line_number(text, badge_hits[0].start()),
            )
        )

    for m in SLOP_RE.finditer(text):
        # Skip matches inside inline code / fences roughly by checking backticks on line
        line_no = _line_number(text, m.start())
        line_text = text.splitlines()[line_no - 1]
        if "`" in line_text and m.group(0) in line_text:
            # still warn — slop in prose near code is rare; keep simple
            pass
        report.add(
            Finding(
                path,
                "warning",
                f"possible AI-slop phrasing {m.group(0)!r}",
                line_no,
            )
        )

    for m in EMOJI_HEADER_RE.finditer(text):
        # Only warn if heading starts with non-alnum after hashes (emoji / symbol)
        title = m.group(0)
        if re.match(r"^#{1,6}\s+[^A-Za-z0-9`\[(]", title):
            report.add(
                Finding(
                    path,
                    "warning",
                    "heading appears to start with emoji/symbol decoration",
                    _line_number(text, m.start()),
                )
            )


def lint_file(path: Path, report: Report) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.add(Finding(path, "error", f"cannot read file: {exc}"))
        return
    lint_text(path, text, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Markdown files to lint (default: README.md)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures",
    )
    args = parser.parse_args(argv)
    paths = args.paths or [Path("README.md")]
    report = Report()

    for path in paths:
        if not path.is_file():
            print(f"ERROR: {path}: not a file", file=sys.stderr)
            return 2
        lint_file(path.resolve(), report)

    for finding in report.findings:
        stream = sys.stderr if finding.kind == "error" else sys.stdout
        print(finding.format(), file=stream)

    err_n = len(report.errors)
    warn_n = len(report.warnings)
    print(f"{err_n} error(s), {warn_n} warning(s)")

    if err_n:
        return 1
    if args.strict and warn_n:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
