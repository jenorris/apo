"""Optima merge — domain_projection sources (supplement legacy schedules)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from apo_engine import domain_projection as dp
from apo_engine import optima_contract, optima_merge, vaults

ET = ZoneInfo("America/New_York")
RULES = (
    "by_kind:\n"
    + "".join(
        f"  {k}:\n    slack: ok\n    email: ok\n    telegram: ok\n    phone: ok\n    rationale: {k}\n"
        for k in ("meeting", "personal", "focus", "free", "default_work")
    )
)


def _id(n: int) -> str:
    return f"{n:024x}"


def _projection(domain: str, blocks: list[tuple[str, datetime, datetime]], *, verified_ago_min: int = 5,
                verified_at: str | None = None) -> dict:
    now = datetime.now(ET)
    today = datetime(now.year, now.month, now.day, tzinfo=ET)
    return {
        "title": f"{domain} schedule projection",
        "status": "active",
        "schema": dp.SCHEMA,
        "domain": domain,
        "timezone": "America/New_York",
        "verified_at": verified_at or (now - timedelta(minutes=verified_ago_min)).isoformat(),
        "coverage": {
            "start": (today - timedelta(days=1)).isoformat(),
            "end": (today + timedelta(days=30)).isoformat(),
            "complete": True,
        },
        "blocks": [
            {"id": _id(i + 1), "kind": k, "start": s.isoformat(), "end": e.isoformat(), "confidence": "medium"}
            for i, (k, s, e) in enumerate(blocks)
        ],
    }


class ProjectionMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "optima"
        self.root.mkdir()
        (self.root / "system" / "contracts").mkdir(parents=True)
        (self.root / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "vault_id: optima\n", encoding="utf-8"
        )
        (self.root / "system" / "config").mkdir(parents=True)
        (self.root / "system" / "config" / "reachability-rules.yaml").write_text(RULES, encoding="utf-8")
        os.environ["APO_VAULT_PATHS"] = str(self.root)
        os.environ["APO_DEFAULT_VAULT"] = "optima"
        vaults._vault_id_cache.clear()
        self.now = datetime.now(ET)

    def tearDown(self) -> None:
        optima_contract.clear_optima_contract_cache()
        for var in ("APO_VAULT_PATHS", "APO_DEFAULT_VAULT", "OPTIMA_SYNC"):
            os.environ.pop(var, None)
        vaults._vault_id_cache.clear()
        self.tmp.cleanup()

    def contract(self, *, projections_if_missing: str = "skip", extra_sources: list[dict] | None = None) -> None:
        sources = list(extra_sources or [])
        for domain in ("atlas", "work"):
            sources.append({
                "id": f"{domain}_projection",
                "path": f"areas/{domain}/projection.yaml",
                "role": "domain_projection",
                "domain": domain,
                "if_missing": projections_if_missing,
            })
        path = self.root / optima_contract.OPTIMA_CONTRACT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({
            "optima_contract_version": "0.9.0",
            "vault_id": "optima",
            "refresh": {
                "watch": {"enabled": True, "interval_seconds": 60},
                "sources": sources,
                "local": {"override": "override.yaml", "if_missing": "skip"},
                "on_all_sources_missing": "degrade_to_free_or_habit",
                "output": {"current": "current.yaml"},
                "reachability_rules": "system/config/reachability-rules.yaml",
            },
        }, sort_keys=False), encoding="utf-8")

    def publish(self, domain: str, payload: dict) -> None:
        p = self.root / "areas" / domain / "projection.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def current(self) -> dict:
        return yaml.safe_load((self.root / "current.yaml").read_text(encoding="utf-8"))

    def span(self, **kw: int) -> tuple[datetime, datetime]:
        return self.now - timedelta(minutes=kw.get("before", 10)), self.now + timedelta(minutes=kw.get("after", 50))

    def test_projection_fills_work_when_no_legacy_source(self) -> None:
        self.contract()
        s, e = self.span()
        self.publish("work", _projection("work", [("meeting", s, e)]))
        result = optima_merge.run_merge(self.root)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["degraded"])
        cur = self.current()
        self.assertEqual(cur["kind"], "meeting")
        self.assertIsNone(cur["theme"])  # projections carry no themes
        self.assertEqual(cur["work"]["calendar"], "work")
        self.assertIn("work-projection", cur["sources"])
        self.assertNotIn("work-schedule", cur["sources"])
        self.assertEqual(cur["domain_projections"]["work"]["state"], "ok")
        self.assertEqual(cur["domain_projections"]["work"]["active"]["kind"], "meeting")
        self.assertEqual(result["projections"]["work"], "ok")

    def test_atlas_family_duty_maps_to_personal_with_family_on_duty(self) -> None:
        self.contract()
        s, e = self.span()
        self.publish("atlas", _projection("atlas", [("family_duty", s, e)]))
        optima_merge.run_merge(self.root)
        cur = self.current()
        self.assertEqual(cur["kind"], "personal")
        self.assertTrue(cur["family"]["on_duty"])

    def test_legacy_source_stays_authoritative(self) -> None:
        work_md = self.root / "legacy-work.md"
        s, e = self.span()
        work_md.write_text(
            f"---\nkind: focus\ntheme: Deep work\nstart: {s.isoformat()}\nend: {e.isoformat()}\n---\n",
            encoding="utf-8",
        )
        self.contract(extra_sources=[{
            "id": "work_theme", "path": "legacy-work.md", "role": "work_theme", "if_missing": "skip",
        }])
        self.publish("work", _projection("work", [("meeting", s, e)]))
        optima_merge.run_merge(self.root)
        cur = self.current()
        self.assertEqual(cur["kind"], "focus")
        self.assertEqual(cur["theme"], "Deep work")
        self.assertIn("work-schedule", cur["sources"])
        self.assertNotIn("work-projection", cur["sources"])
        self.assertEqual(cur["domain_projections"]["work"]["state"], "ok")  # health still reported

    def test_gap_is_not_free_fallback_but_is_reported(self) -> None:
        self.contract()
        later = self.now + timedelta(hours=3)
        self.publish("work", _projection("work", [("meeting", later, later + timedelta(hours=1))]))
        result = optima_merge.run_merge(self.root)
        self.assertTrue(result["degraded"])  # no schedule source produced a block
        summary = self.current()["domain_projections"]["work"]
        self.assertEqual(summary["position"], "none")
        self.assertEqual(summary["next"]["kind"], "meeting")

    def test_invalid_projection_is_reported_and_never_used(self) -> None:
        self.contract()
        s, e = self.span()
        bad = _projection("work", [("meeting", s, e)], verified_at=(self.now + timedelta(hours=2)).isoformat())
        self.publish("work", bad)
        result = optima_merge.run_merge(self.root)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["degraded"])
        self.assertEqual(result["projections"]["work"], "error")
        summary = self.current()["domain_projections"]["work"]
        self.assertEqual(summary["state"], "error")
        self.assertIn("future", summary["error"])

    def test_unparseable_yaml_is_an_error_not_empty(self) -> None:
        self.contract()
        p = self.root / "areas" / "work" / "projection.yaml"
        p.parent.mkdir(parents=True)
        p.write_text("blocks: [unclosed\n", encoding="utf-8")
        optima_merge.run_merge(self.root)
        self.assertEqual(self.current()["domain_projections"]["work"]["state"], "error")

    def test_missing_projection_is_reported_as_missing(self) -> None:
        self.contract()
        result = optima_merge.run_merge(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(self.current()["domain_projections"]["work"], {"state": "missing"})

    def test_if_missing_error_fails_the_merge(self) -> None:
        self.contract(projections_if_missing="error")
        result = optima_merge.run_merge(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "source_error")

    def test_if_missing_error_fails_on_invalid_projection(self) -> None:
        self.contract(projections_if_missing="error")
        s, e = self.span()
        self.publish("atlas", _projection("atlas", [], verified_at="2020-01-01T00:00:00"))
        self.publish("work", _projection("work", [("meeting", s, e)]))
        result = optima_merge.run_merge(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("invalid source atlas_projection", result["message"])

    def test_stale_projection_is_still_used_but_flagged(self) -> None:
        self.contract()
        s, e = self.span()
        self.publish("work", _projection("work", [("meeting", s, e)], verified_ago_min=600))
        optima_merge.run_merge(self.root)
        cur = self.current()
        self.assertEqual(cur["kind"], "meeting")
        self.assertTrue(cur["domain_projections"]["work"]["stale"])

    def test_unchanged_projection_does_not_rewrite_current(self) -> None:
        self.contract()
        s, e = self.span()
        self.publish("work", _projection("work", [("meeting", s, e)]))
        self.assertTrue(optima_merge.run_merge(self.root)["wrote"])
        self.assertFalse(optima_merge.run_merge(self.root)["wrote"], "projection summary must be tick-stable")

    def test_no_projection_sources_leaves_payload_unchanged(self) -> None:
        path = self.root / optima_contract.OPTIMA_CONTRACT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({
            "optima_contract_version": "0.9.0", "vault_id": "optima",
            "refresh": {"watch": {"enabled": True}, "sources": []},
        }), encoding="utf-8")
        optima_merge.run_merge(self.root)
        self.assertNotIn("domain_projections", self.current())


class ContractTest(unittest.TestCase):
    def test_source_spec_reads_domain_and_stale_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / optima_contract.OPTIMA_CONTRACT_REL
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump({"refresh": {"sources": [
                {"id": "a", "path": "areas/atlas/projection.yaml", "role": "domain_projection",
                 "domain": "atlas", "stale_after_minutes": 30},
                {"id": "b", "path": "areas/work/projection.yaml", "role": "domain_projection",
                 "domain": "work", "stale_after_minutes": "nope"},
            ]}}), encoding="utf-8")
            try:
                a, b = optima_contract.merge_settings(root).sources
            finally:
                optima_contract.clear_optima_contract_cache()
            self.assertEqual((a.domain, a.stale_after_minutes), ("atlas", 30))
            self.assertEqual((b.domain, b.stale_after_minutes), ("work", 120))

    def test_check_contract_flags_missing_or_bad_domain(self) -> None:
        def findings(**row):
            return optima_contract.check_contract({"refresh": {"sources": [
                {"path": "p.yaml", "role": "domain_projection", **row}]}})
        self.assertTrue(any("domain" in f["field"] for f in findings()))
        self.assertTrue(any("domain" in f["field"] for f in findings(domain="mars")))
        self.assertEqual(findings(domain="atlas"), [])


if __name__ == "__main__":
    unittest.main()
