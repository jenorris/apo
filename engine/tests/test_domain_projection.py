"""Optima domain projection exchange v1 — parse, resolve, summarize."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apo_engine import domain_projection as dp

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=ET)


def _id(n: int) -> str:
    return f"{n:024x}"


def _block(n: int, kind: str, start: datetime, end: datetime, conf: str = "medium") -> dict:
    return {
        "id": _id(n),
        "kind": kind,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "confidence": conf,
    }


def _payload(blocks: list[dict] | None = None, *, domain: str = "work") -> dict:
    return {
        "title": f"{domain} schedule projection",
        "status": "active",
        "schema": dp.SCHEMA,
        "domain": domain,
        "timezone": "America/New_York",
        "verified_at": (NOW - timedelta(minutes=5)).isoformat(),
        "coverage": {
            "start": datetime(2026, 9, 30, tzinfo=ET).isoformat(),
            "end": datetime(2026, 10, 30, tzinfo=ET).isoformat(),
            "complete": True,
        },
        "blocks": blocks if blocks is not None else [],
    }


def _parse(payload: dict, domain: str = "work") -> dp.Projection:
    return dp.parse(payload, expect_domain=domain, now=NOW)


class ParseTest(unittest.TestCase):
    def test_valid_empty_and_populated(self) -> None:
        self.assertEqual(_parse(_payload()).blocks, ())
        p = _parse(_payload([_block(1, "meeting", NOW, NOW + timedelta(hours=1))]))
        self.assertEqual(p.blocks[0].kind, "meeting")

    def test_yaml_native_datetimes_accepted(self) -> None:
        payload = _payload()
        payload["verified_at"] = NOW - timedelta(minutes=1)
        self.assertEqual(_parse(payload).domain, "work")

    def test_rejections(self) -> None:
        bad_cases = {
            "extra top key": lambda p: p.update(extra=1),
            "missing top key": lambda p: p.pop("title"),
            "wrong schema": lambda p: p.update(schema="v0"),
            "domain at wrong path": lambda p: p.update(domain="atlas"),
            "bad timezone": lambda p: p.update(timezone="Mars/Base"),
            "future verified_at": lambda p: p.update(
                verified_at=(NOW + timedelta(hours=1)).isoformat()
            ),
            "naive verified_at": lambda p: p.update(verified_at="2026-09-30T11:00:00"),
            "incomplete coverage": lambda p: p["coverage"].update(complete=False),
            "inverted coverage": lambda p: p["coverage"].update(
                end=p["coverage"]["start"]
            ),
            "blocks not list": lambda p: p.update(blocks={}),
        }
        for name, mutate in bad_cases.items():
            with self.subTest(name):
                payload = copy.deepcopy(_payload())
                mutate(payload)
                with self.assertRaises(dp.ProjectionError):
                    _parse(payload)

    def test_block_rejections(self) -> None:
        later = NOW + timedelta(hours=2)
        good = _block(1, "meeting", NOW, NOW + timedelta(hours=1))
        bad_cases = {
            "extra key (title leak)": {**good, "title": "Board"},
            "bad id": {**good, "id": "XYZ"},
            "unknown kind": {**good, "kind": "vacation"},
            "unknown confidence": {**good, "confidence": "certain"},
            "empty block": _block(1, "meeting", NOW, NOW),
            "outside coverage": _block(
                1, "meeting", datetime(2026, 9, 1, tzinfo=ET), datetime(2026, 9, 2, tzinfo=ET)
            ),
        }
        for name, block in bad_cases.items():
            with self.subTest(name):
                with self.assertRaises(dp.ProjectionError):
                    _parse(_payload([block]))
        with self.subTest("unsorted"):
            with self.assertRaises(dp.ProjectionError):
                _parse(_payload([_block(1, "meeting", later, later + timedelta(hours=1)), good]))
        with self.subTest("duplicate id"):
            with self.assertRaises(dp.ProjectionError):
                _parse(_payload([good, _block(1, "focus", later, later + timedelta(hours=1))]))


class ResolveTest(unittest.TestCase):
    def test_highest_precedence_block_wins_and_overlaps_retained(self) -> None:
        p = _parse(
            _payload(
                [
                    _block(1, "personal", NOW - timedelta(hours=1), NOW + timedelta(hours=1)),
                    _block(2, "meeting", NOW, NOW + timedelta(minutes=30)),
                ]
            )
        )
        res = dp.resolve(p, NOW)
        self.assertEqual(res.state, "active")
        self.assertEqual(res.active.kind, "meeting")

    def test_family_duty_outranks_personal(self) -> None:
        p = _parse(
            _payload(
                [
                    _block(1, "personal", NOW - timedelta(hours=1), NOW + timedelta(hours=1)),
                    _block(2, "family_duty", NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)),
                ],
                domain="atlas",
            ),
            "atlas",
        )
        self.assertEqual(dp.resolve(p, NOW).active.kind, "family_duty")

    def test_gap_inside_coverage_is_none_not_free(self) -> None:
        p = _parse(_payload([_block(1, "meeting", NOW + timedelta(hours=1), NOW + timedelta(hours=2))]))
        res = dp.resolve(p, NOW)
        self.assertEqual(res.state, "none")
        self.assertIsNone(res.active)
        self.assertEqual(res.next_block.kind, "meeting")

    def test_outside_coverage_is_unknown(self) -> None:
        p = _parse(_payload())
        self.assertEqual(dp.resolve(p, datetime(2026, 12, 1, tzinfo=ET)).state, "outside_coverage")
        self.assertEqual(dp.resolve(p, datetime(2026, 9, 29, tzinfo=ET)).state, "outside_coverage")

    def test_bounds_are_half_open(self) -> None:
        end = NOW + timedelta(hours=1)
        p = _parse(_payload([_block(1, "meeting", NOW, end)]))
        self.assertEqual(dp.resolve(p, NOW).state, "active")
        self.assertEqual(dp.resolve(p, end).state, "none")


class SummarizeTest(unittest.TestCase):
    def test_stale_flag(self) -> None:
        p = _parse(_payload())
        self.assertFalse(dp.summarize(p, NOW, stale_after_minutes=120)["stale"])
        self.assertTrue(dp.summarize(p, NOW + timedelta(hours=3), stale_after_minutes=120)["stale"])

    def test_summary_is_stable_between_ticks(self) -> None:
        # Ages would change every tick and defeat the merge's unchanged-payload skip.
        p = _parse(_payload([_block(1, "meeting", NOW - timedelta(minutes=10), NOW + timedelta(hours=1))]))
        a = dp.summarize(p, NOW, stale_after_minutes=120)
        b = dp.summarize(p, NOW + timedelta(minutes=1), stale_after_minutes=120)
        self.assertEqual(a, b)
        self.assertFalse([k for k in a if k.startswith("age")])


if __name__ == "__main__":
    unittest.main()
