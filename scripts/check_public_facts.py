#!/usr/bin/env python3
"""Check documented MCP counts against the live FastMCP registry."""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path
from typing import NamedTuple


class PublicFacts(NamedTuple):
    top_level_tools: int
    admin_capabilities: int


def runtime_facts() -> PublicFacts:
    """Read counts from the decorated server and its public admin catalog."""
    from apo_engine import apo_admin
    from apo_engine.mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    capabilities = {
        item["name"] for item in apo_admin.admin_list()["capabilities"]
    }
    missing_handlers = capabilities - server._ADMIN_HANDLERS.keys()
    if missing_handlers:
        missing = ", ".join(sorted(missing_handlers))
        raise RuntimeError(f"admin capabilities without handlers: {missing}")
    return PublicFacts(len(tools), len(capabilities))


def check_public_facts(root: Path, facts: PublicFacts) -> list[str]:
    """Return every stale or missing public count."""
    errors: list[str] = []
    readme = (root / "README.md").read_text(encoding="utf-8")
    justfile = (root / "justfile").read_text(encoding="utf-8")

    summary = re.search(
        r"\*\*MCP surface\*\*\s*\|\s*(\d+) top-level tools \+ "
        r"(\d+) admin capabilities",
        readme,
    )
    if summary is None:
        errors.append("README.md MCP surface count is missing")
    else:
        documented = tuple(map(int, summary.groups()))
        actual = (facts.top_level_tools, facts.admin_capabilities)
        if documented != actual:
            errors.append(
                "README.md MCP surface says "
                f"{documented[0]}/{documented[1]}; runtime is {actual[0]}/{actual[1]}"
            )

    table_top = re.search(r"\| Top-level \| \*\*(\d+)\*\*", readme)
    table_admin = re.search(r"\| Via `apo_admin` \| \*\*(\d+)\*\*", readme)
    if table_top is None or table_admin is None:
        errors.append("README.md MCP tools table counts are missing")
    else:
        documented = (int(table_top.group(1)), int(table_admin.group(1)))
        actual = (facts.top_level_tools, facts.admin_capabilities)
        if documented != actual:
            errors.append(
                "README.md MCP tools table says "
                f"{documented[0]}/{documented[1]}; runtime is {actual[0]}/{actual[1]}"
            )

    just_count = re.search(
        r"Pure-Python MCP tool count \(no Node\)\. Expect (\d+) tools\.",
        justfile,
    )
    if just_count is None:
        errors.append("justfile tool-list help count is missing")
    elif int(just_count.group(1)) != facts.top_level_tools:
        errors.append(
            f"justfile tool-list help says {just_count.group(1)} tools; "
            f"runtime is {facts.top_level_tools}"
        )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    facts = runtime_facts()
    errors = check_public_facts(args.root, facts)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"Public facts OK: {facts.top_level_tools} top-level tools, "
        f"{facts.admin_capabilities} admin capabilities"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
