"""Public MCP counts must be derived from the live server and stay documented."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CHECKER = _ROOT / "scripts" / "check_public_facts.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_public_facts", _CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_documented_public_facts_match_runtime(tmp_path, monkeypatch) -> None:
    checker = _load_checker()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("APO_NOTES_ROOT", str(vault))
    monkeypatch.setenv("APO_INDEX", str(tmp_path / "index.db"))
    monkeypatch.setenv("APO_COLLECTION", "public_facts_test")

    facts = checker.runtime_facts()
    assert facts.top_level_tools == 12
    assert facts.admin_capabilities == 7
    assert checker.check_public_facts(_ROOT, facts) == []


@pytest.mark.parametrize(
    ("document", "expected_error"),
    [
        (
            "| **MCP surface** | 11 top-level tools + 7 admin capabilities via `apo_admin` |",
            "README.md MCP surface says 11/7; runtime is 12/7",
        ),
        (
            "# Pure-Python MCP tool count (no Node). Expect 15 tools.",
            "justfile tool-list help says 15 tools; runtime is 12",
        ),
    ],
)
def test_checker_reports_stale_counts(
    tmp_path: Path, document: str, expected_error: str
) -> None:
    checker = _load_checker()
    (tmp_path / "README.md").write_text(document, encoding="utf-8")
    (tmp_path / "justfile").write_text(document, encoding="utf-8")

    errors = checker.check_public_facts(
        tmp_path, checker.PublicFacts(top_level_tools=12, admin_capabilities=7)
    )

    assert expected_error in errors
