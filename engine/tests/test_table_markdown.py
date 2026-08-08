"""GFM table parse / serialize / flatten / fuzzy-header tests."""

from __future__ import annotations

import unittest

from apo_engine.table_markdown import (
    HeaderAmbiguous,
    csv_to_gfm,
    csv_to_records,
    find_tables,
    fuzzy_header_map,
    header_flatten_text,
    json_to_gfm,
    normalize_header,
    records_to_gfm,
    row_flatten_text,
    row_key_for,
    table_id_for,
    table_schema_hash,
)

DOC = """## Maintenance History

Some prose.

| Date       | Mileage | Service      |
| ---------- | ------: | ------------ |
| 2026-06-07 |  114587 | Brake flush  |
| 2026-07-01 |  115200 | Oil change   |

Trailing prose.

```
| Not | A | Table |
| --- | --- | --- |
| in  | code | fence |
```
"""


class TestParse(unittest.TestCase):
    def test_find_one_table_skip_fence(self):
        tables = find_tables(DOC.split("\n"))
        self.assertEqual(len(tables), 1)
        t = tables[0]
        self.assertEqual(t.headers, ["Date", "Mileage", "Service"])
        self.assertEqual(len(t.rows), 2)
        self.assertEqual(t.rows[0], ["2026-06-07", "114587", "Brake flush"])
        self.assertEqual(t.alignments[1], "right")

    def test_escaped_pipe(self):
        text = "| a | b |\n| --- | --- |\n| x \\| y | z |\n"
        t = find_tables(text.split("\n"))[0]
        self.assertEqual(t.rows[0], ["x | y", "z"])

    def test_ragged_row_padded(self):
        text = "| a | b | c |\n| --- | --- | --- |\n| 1 | 2 |\n"
        t = find_tables(text.split("\n"))[0]
        self.assertEqual(t.rows[0], ["1", "2", ""])


class TestSerialize(unittest.TestCase):
    def test_roundtrip(self):
        t = find_tables(DOC.split("\n"))[0]
        gfm = records_to_gfm(t.headers, t.rows, alignments=t.alignments)
        t2 = find_tables(gfm.split("\n"))[0]
        self.assertEqual(t2.headers, t.headers)
        self.assertEqual(t2.rows, t.rows)

    def test_csv_to_gfm(self):
        gfm = csv_to_gfm("date,mileage,service\n2026-06-07,114587,Brake flush\n")
        t = find_tables(gfm.split("\n"))[0]
        self.assertEqual(t.headers, ["date", "mileage", "service"])
        self.assertEqual(t.rows[0], ["2026-06-07", "114587", "Brake flush"])

    def test_json_list_to_gfm(self):
        gfm = json_to_gfm([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
        t = find_tables(gfm.split("\n"))[0]
        self.assertEqual(t.headers, ["a", "b"])
        self.assertEqual(t.rows[1], ["3", "4"])


class TestFlatten(unittest.TestCase):
    def test_row_flatten(self):
        s = row_flatten_text(
            ["Pacifica", "Maintenance History"],
            ["Date", "Mileage", "Service"],
            ["2026-06-07", "114587", "Brake flush"],
        )
        self.assertEqual(
            s,
            "Pacifica > Maintenance History — Date: 2026-06-07, Mileage: 114587, Service: Brake flush",
        )

    def test_header_flatten(self):
        s = header_flatten_text(["Log"], ["Date", "Mileage"])
        self.assertEqual(s, "Log — Columns: Date, Mileage")


class TestKeysHashes(unittest.TestCase):
    def test_row_key_first_column(self):
        t = find_tables(DOC.split("\n"))[0]
        self.assertEqual(row_key_for(t, 0), "2026-06-07")

    def test_row_key_named_column(self):
        t = find_tables(DOC.split("\n"))[0]
        self.assertEqual(row_key_for(t, 0, key_column="Service"), "Brake flush")

    def test_schema_hash_changes_on_rename(self):
        h1 = table_schema_hash(["Date", "Mileage"])
        h2 = table_schema_hash(["Date", "Miles"])
        self.assertNotEqual(h1, h2)

    def test_table_id_stable(self):
        self.assertEqual(table_id_for("a.md", 5, 0), table_id_for("a.md", 5, 0))
        self.assertNotEqual(table_id_for("a.md", 5, 0), table_id_for("a.md", 5, 1))


class TestFuzzyHeaders(unittest.TestCase):
    def test_exact_normalized_match(self):
        m = fuzzy_header_map(["Date", "mileage"], ["date", "Mileage", "Service"])
        self.assertEqual(m["Date"], "date")
        self.assertEqual(m["mileage"], "Mileage")

    def test_close_match(self):
        m = fuzzy_header_map(["servce"], ["Date", "Service"])
        self.assertEqual(m["servce"], "Service")

    def test_ambiguous_rejects(self):
        with self.assertRaises(HeaderAmbiguous) as ctx:
            fuzzy_header_map(["xyz"], ["Date", "Service"])
        self.assertTrue(ctx.exception.suggestions)

    def test_allow_new_columns(self):
        m = fuzzy_header_map(["notes"], ["Date"], allow_new_columns=True)
        self.assertEqual(m["notes"], "notes")

    def test_normalize(self):
        self.assertEqual(normalize_header("  Last Checked! "), "last_checked")


if __name__ == "__main__":
    unittest.main()
