"""Compatibility seams that must survive decomposition of ``apo_engine.core``."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from apo_engine import (
    chunking,
    content,
    core,
    embeddings,
    index_db,
    index_models,
    indexing,
    link_index,
    mcp_backend,
    ops,
    ranking,
    retrieval,
)


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
        "assert ranking.Hit is core.Hit if 'ranking' in globals() else True\n"
        "assert mcp_backend.core is core if 'mcp_backend' in globals() else True\n"
        "assert ops.core is core if 'ops' in globals() else True\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_core_module_identity_is_shared_by_dependents() -> None:
    assert ranking.Hit is core.Hit
    assert index_models.Hit is core.Hit
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


def test_core_exports_are_owned_by_domain_modules() -> None:
    assert core.embed is embeddings.embed
    assert core.query_embed is embeddings.query_embed
    assert core.reader_connect is index_db.reader_connect
    assert core.writer_connect is index_db.writer_connect
    assert core.note_catalog_json is content.note_catalog_json
    assert core._markdown_chunk_rows is chunking._markdown_chunk_rows
    assert core.resolve_links is link_index.resolve_links
    assert core.index_vault is indexing.index_vault
    assert core.index_file is indexing.index_file
    assert core.search is retrieval.search
    assert core.filter_notes is retrieval.filter_notes


def test_no_replacement_god_module() -> None:
    limits = {
        indexing: 1500,
        index_db: 1500,
        embeddings: 1500,
        retrieval: 1800,
        chunking: 1500,
        content: 1500,
        link_index: 1500,
    }
    for module, limit in limits.items():
        lines = Path(module.__file__).read_text(encoding="utf-8").count("\n") + 1
        assert lines <= limit, (module.__name__, lines, limit)


def test_lower_level_modules_do_not_import_core() -> None:
    package = Path(core.__file__).parent
    modules = (
        "index_models.py",
        "content.py",
        "chunking.py",
        "embeddings.py",
        "index_db.py",
        "link_index.py",
        "indexing.py",
        "retrieval.py",
        "ref_catalog.py",
        "git_catalog.py",
    )
    for name in modules:
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert not any(
            (
                isinstance(node, ast.Import)
                and any(alias.name == "apo_engine.core" for alias in node.names)
            )
            or (
                isinstance(node, ast.ImportFrom)
                and node.module in {"apo_engine", "", None}
                and any(alias.name == "core" for alias in node.names)
            )
            or (
                isinstance(node, ast.ImportFrom)
                and node.module in {"core", "apo_engine.core"}
            )
            for node in imports
        ), name


def test_ranking_does_not_import_core() -> None:
    tree = ast.parse(Path(ranking.__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom)
        and (
            node.module in {"core", "apo_engine.core"}
            or (
                node.module in {"apo_engine", ""}
                and any(alias.name == "core" for alias in node.names)
            )
        )
        for node in ast.walk(tree)
    )
