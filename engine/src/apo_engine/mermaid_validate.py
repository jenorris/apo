"""Mermaid syntax validation — parse-time flaws for index and write paths."""

from __future__ import annotations

from typing import Any

from . import mermaid_contract as mc
from . import mermaid_parse as mp
from . import vaults
from .note_lint import Flaw


def validate_mermaid_text(text: str, rel: str = "") -> list[Flaw]:
    """Return structured flaws for mermaid source."""
    flaws: list[Flaw] = []
    diagram = mp.parse_mermaid(text or "")
    if diagram.parse_error:
        flaws.append(
            Flaw(
                code="mermaid.syntax",
                severity="error",
                path=rel,
                message=f"Mermaid parse failed: {diagram.parse_error}",
                evidence={"code": "MERMAID_PARSE"},
                remediation="llm",
            )
        )
    for w in diagram.parse_warnings:
        flaws.append(
            Flaw(
                code="mermaid.syntax",
                severity="warn",
                path=rel,
                message=w,
                evidence={"code": "MERMAID_WARN"},
                remediation="llm",
            )
        )
    if rel:
        root = vaults.notes_root()
        cat_rule = mc.catalog_rule_for(root, rel)
        if cat_rule and not mc.catalog_entry_for(root, rel):
            flaws.append(
                Flaw(
                    code="mermaid.catalog_missing",
                    severity="warn",
                    path=rel,
                    message="catalog.yaml has no entry for this diagram slug",
                    evidence={"catalog_path": cat_rule.get("catalog_path")},
                    remediation="human",
                )
            )
    return flaws


def flaws_as_dicts(text: str, rel: str = "") -> list[dict[str, Any]]:
    return [f.as_dict() for f in validate_mermaid_text(text, rel)]


def should_block_write(text: str, rel: str) -> tuple[bool, list[dict[str, Any]]]:
    """Hard validation mode blocks writes when parse fails."""
    root = vaults.notes_root()
    mode = mc.validation_mode(root, rel)
    flaws = validate_mermaid_text(text, rel)
    dicts = [f.as_dict() for f in flaws]
    if mode != "hard":
        return False, dicts
    for f in flaws:
        if f.severity == "error":
            return True, dicts
    return False, dicts
