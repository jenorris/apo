"""Compatibility seams that must survive decomposition of ``apo_engine.core``."""

from __future__ import annotations

import subprocess
import sys

import pytest

from apo_engine import core, mcp_backend, ops, ranking


@pytest.mark.parametrize(
    "imports",
    [
        "from apo_engine import core, ranking",
        "from apo_engine import ranking, core",
        "from apo_engine import core, mcp_backend",
        "from apo_engine import mcp_backend, core",
        "from apo_engine import core, ops",
        "from apo_engine import ops, core",
    ],
)
def test_core_import_order_is_safe(imports: str) -> None:
    script = (
        f"{imports}\n"
        "assert ranking.core is core if 'ranking' in globals() else True\n"
        "assert mcp_backend.core is core if 'mcp_backend' in globals() else True\n"
        "assert ops.core is core if 'ops' in globals() else True\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_core_module_identity_is_shared_by_dependents() -> None:
    assert ranking.core is core
    assert mcp_backend.core is core
    assert ops.core is core
    assert mcp_backend.core.Hit is core.Hit


def test_public_embed_patch_point_is_resolved_at_call_time(monkeypatch) -> None:
    replacement = object()
    monkeypatch.setattr(core, "embed", replacement)

    assert core.index_file.__globals__["embed"] is replacement


def test_public_query_embed_patch_point_is_resolved_at_call_time(monkeypatch) -> None:
    replacement = object()
    monkeypatch.setattr(core, "query_embed", replacement)

    assert core.search.__globals__["query_embed"] is replacement
