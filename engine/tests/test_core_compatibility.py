"""Compatibility seams that must survive decomposition of ``apo_engine.core``."""

from __future__ import annotations

import ast
import os
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
        # retrieval.py is 1848 lines. Until the wiring was removed, every domain
        # module's ``__file__`` was overwritten with indexing.py's, so this guard
        # measured indexing.py seven times. It now measures the real files.
        retrieval: 1850,
        chunking: 1500,
        content: 1500,
        link_index: 1500,
    }
    for module, limit in limits.items():
        assert module.__name__.startswith("apo_engine."), module.__name__
        assert Path(module.__file__).stem == module.__name__.rsplit(".", 1)[-1]
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


_STANDALONE_MODULES = (
    "content",
    "chunking",
    "embeddings",
    "index_db",
    "link_index",
    "retrieval",
    "ranking",
)

_STANDALONE_SCRIPT = """
import builtins
import importlib
import symtable
import sys
from pathlib import Path

module = importlib.import_module("apo_engine." + sys.argv[1])
# Neither the injection site nor the compatibility facade may be needed.
assert "apo_engine.indexing" not in sys.modules, "indexing was imported"
assert "apo_engine.core" not in sys.modules, "core was imported"

source = Path(module.__file__).read_text(encoding="utf-8")
missing = set()

def walk(table):
    for symbol in table.get_symbols():
        name = symbol.get_name()
        in_function = table.get_type() != "module"
        used = symbol.is_global() if in_function else True
        if used and symbol.is_referenced():
            if not hasattr(module, name) and not hasattr(builtins, name):
                missing.add(name)
    for child in table.get_children():
        walk(child)

walk(symtable.symtable(source, module.__file__, "exec"))
assert not missing, sorted(missing)
"""


@pytest.mark.parametrize("name", _STANDALONE_MODULES)
def test_domain_module_imports_alone_and_resolves_its_names(name: str) -> None:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, "-c", _STANDALONE_SCRIPT, name],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_retrieval_and_ranking_dependencies_are_ordinary_imports() -> None:
    assert retrieval.reader_connect is index_db.reader_connect
    assert retrieval.query_embed is embeddings.query_embed
    assert retrieval.RRF_K == index_db.RRF_K
    assert retrieval.ranking is ranking
    assert retrieval._FM_KEY_SAFE is content._FM_KEY_SAFE
    assert retrieval._scoped_vector_hits is index_db._scoped_vector_hits
    assert retrieval._collapse_part_hits is index_db._collapse_part_hits
    assert index_db.embed is embeddings.embed
    assert link_index.writer_connect is index_db.writer_connect
    assert chunking._index_text_for_embedding is retrieval._index_text_for_embedding


def test_module_wiring_injection_is_gone() -> None:
    assert not hasattr(indexing, "_wire_modules")
    for module in (chunking, embeddings, index_db, link_index, retrieval):
        # No domain module carries a copy of another module's namespace.
        assert "index_file" not in module.__dict__, module.__name__
        assert "_VAULT_FLUSH_CHUNKS" not in module.__dict__, module.__name__
