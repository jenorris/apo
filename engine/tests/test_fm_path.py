"""Frontmatter path grammar — maps, list indices, id selectors."""

from __future__ import annotations

import unittest

from apo_engine.fm_path import (
    FmPathError,
    IdSelector,
    ListIndex,
    MapKey,
    delete_at_path,
    resolve_values,
    set_at_path,
    split_path,
)
from apo_engine.markdown_patch import PatchError
from apo_engine.yaml_patch import delete_field_path, set_field_path


class TestSplitPath(unittest.TestCase):
    def test_simple_and_dotted(self):
        self.assertEqual(split_path("status"), [MapKey("status")])
        self.assertEqual(split_path("meta.owner"), [MapKey("meta"), MapKey("owner")])

    def test_list_index(self):
        parts = split_path("todos.0.status")
        self.assertEqual(parts, [MapKey("todos"), ListIndex(0), MapKey("status")])

    def test_id_selector(self):
        parts = split_path("todos[id=skypad-resolver].status")
        self.assertEqual(
            parts,
            [
                MapKey("todos"),
                IdSelector("id", "skypad-resolver"),
                MapKey("status"),
            ],
        )

    def test_empty_invalid(self):
        with self.assertRaises(FmPathError) as ctx:
            split_path("")
        self.assertEqual(ctx.exception.code, "invalid_op")


class TestMutate(unittest.TestCase):
    def test_set_by_id_and_index(self):
        data = {
            "todos": [
                {"id": "a", "status": "pending"},
                {"id": "b", "status": "pending"},
            ]
        }
        set_at_path(data, "todos[id=a].status", "completed")
        self.assertEqual(data["todos"][0]["status"], "completed")
        set_at_path(data, "todos.1.status", "done")
        self.assertEqual(data["todos"][1]["status"], "done")

    def test_create_list_index_path(self):
        data: dict = {}
        set_at_path(data, "todos.0.status", "pending")
        self.assertEqual(data["todos"][0]["status"], "pending")

    def test_replace_whole_list(self):
        data = {"todos": [{"id": "old"}]}
        set_at_path(data, "todos", [{"id": "new", "status": "pending"}])
        self.assertEqual(data["todos"][0]["id"], "new")

    def test_delete_by_id(self):
        data = {"todos": [{"id": "a"}, {"id": "b"}]}
        delete_at_path(data, "todos[id=a]")
        self.assertEqual(len(data["todos"]), 1)
        self.assertEqual(data["todos"][0]["id"], "b")

    def test_ambiguous_id(self):
        data = {"todos": [{"id": "x"}, {"id": "x"}]}
        with self.assertRaises(FmPathError) as ctx:
            set_at_path(data, "todos[id=x].status", "done")
        self.assertEqual(ctx.exception.code, "anchor_ambiguous")

    def test_yaml_patch_wrappers(self):
        data = {"todos": [{"id": "a", "status": "pending"}]}
        set_field_path(data, "todos[id=a].status", "completed")
        self.assertEqual(data["todos"][0]["status"], "completed")
        delete_field_path(data, "todos[id=a]")
        self.assertEqual(data["todos"], [])
        with self.assertRaises(PatchError) as ctx:
            delete_field_path(data, "todos[id=missing]")
        self.assertEqual(ctx.exception.code, "anchor_not_found")


class TestResolveValues(unittest.TestCase):
    def test_list_field_sugar(self):
        data = {
            "todos": [
                {"id": "a", "status": "pending"},
                {"id": "b", "status": "completed"},
            ]
        }
        self.assertEqual(
            set(resolve_values(data, "todos.status")),
            {"pending", "completed"},
        )
        self.assertEqual(resolve_values(data, "todos.0.id"), ["a"])
        self.assertEqual(resolve_values(data, "missing.path"), [])


if __name__ == "__main__":
    unittest.main()
