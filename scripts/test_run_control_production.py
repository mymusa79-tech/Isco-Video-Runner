from __future__ import annotations

import copy
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from scripts import run_control_production as control


class ControlRequestProductionTests(unittest.TestCase):
    def _request(self, *, kind: str = "short", scope: str = "short_only") -> dict:
        request = {
            "schema_version": 1,
            "request_id": "req-123",
            "source": "telegram_editorial_control_panel",
            "kind": kind,
            "approval_scope": scope,
            "approved_by_user": True,
            "approved_at": "2026-08-22T12:00:00+00:00",
            "approved_topic": "موضوع معتمد",
            "format": "moment" if kind == "short" else "film",
            "weekly_option_id": "telegram:s:1",
            "content_boundaries": [],
            "production_dispatch_authorized": False,
            "status": "approved_waiting_production_activation",
        }
        request["request_sha256"] = control._canonical_request_hash(request)
        return request

    def test_exact_hash_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "request.json"
            request = self._request()
            path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            loaded = control.load_control_request(path, request["request_sha256"])
            self.assertEqual(loaded["request_id"], "req-123")
            tampered = copy.deepcopy(request)
            tampered["approved_topic"] = "موضوع آخر"
            path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after Telegram approval"):
                control.load_control_request(path, request["request_sha256"])

    def test_stored_request_can_never_arrive_dispatch_authorized(self):
        request = self._request()
        request["production_dispatch_authorized"] = True
        request["request_sha256"] = control._canonical_request_hash(request)
        with self.assertRaisesRegex(RuntimeError, "must remain non-dispatching"):
            control.validate_control_request(request, request["request_sha256"])

    def test_short_brief_can_be_materialized_without_long_research_pack(self):
        with tempfile.TemporaryDirectory() as temp:
            path, digest = control.materialize_approved_brief(self._request(), Path(temp) / "brief.json")
            brief = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(brief["format"], "moment")
            self.assertEqual(brief["research_pack"], [])
            self.assertEqual(brief["approved_hash"], digest)
            self.assertTrue(brief["approved_by_user"])

    def test_long_brief_fails_closed_without_completed_research_pack(self):
        request = self._request(kind="long", scope="long_only")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "approved research pack"):
                control.materialize_approved_brief(request, Path(temp) / "brief.json")

    def test_long_brief_accepts_two_structured_sources(self):
        request = self._request(kind="long", scope="long_only")
        request["approved_research_pack"] = [
            {"source_title": "Source A", "source_url": "https://example.com/a", "claim_scope": "background"},
            {"source_title": "Source B", "source_url": "https://example.com/b", "claim_scope": "background"},
        ]
        request["request_sha256"] = control._canonical_request_hash(request)
        with tempfile.TemporaryDirectory() as temp:
            path, _ = control.materialize_approved_brief(request, Path(temp) / "brief.json")
            brief = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(brief["research_pack"]), 2)

    def test_sibling_plan_selects_distinct_jobs_without_dispatch_and_hashes_exact_long_plan(self):
        request = self._request(kind="long", scope="long_plus_sibling_shorts")
        request["sibling_shorts"] = {"minimum": 2, "maximum": 3}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "sections": [
                            {"key_point": "الفكرة الأولى"},
                            {"key_point": "الفكرة الثانية"},
                            {"key_point": "الفكرة الثالثة"},
                            {"key_point": "الفكرة الأولى"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            path = control.write_sibling_short_plan(root, request)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["short_count"], 3)
            self.assertEqual(data["source_request_sha256"], request["request_sha256"])
            self.assertEqual(data["source_production_plan_sha256"], control._sha256_file(plan_path))
            self.assertEqual(len(data["source_production_plan_sha256"]), 64)
            self.assertFalse(data["automatic_production_started"])
            self.assertTrue(all(item["production_dispatch_authorized"] is False for item in data["semantic_jobs"]))
            self.assertEqual(data["youtube_publish_mode"], "manual_in_youtube_studio")

    def test_sibling_plan_refuses_to_fabricate_minimum_quota(self):
        request = self._request(kind="long", scope="long_plus_sibling_shorts")
        request["sibling_shorts"] = {"minimum": 2, "maximum": 3}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "plan.json").write_text(
                json.dumps({"sections": [{"key_point": "نفس الفكرة"}, {"key_point": "نفس الفكرة"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "enough independent jobs"):
                control.write_sibling_short_plan(root, request)

    def test_execute_control_request_patches_the_live_install_router_seam(self):
        # Regression for the 2026-08-31 Telegram outage: production.install_router
        # (scripts.run_v3_voice.install_router) no longer exists since the planning
        # seam consolidation - production.main() resolves install_router from
        # scripts.planning_runtime_contract's own module globals instead. Patching
        # production.install_router is a dead reference that crashes with
        # AttributeError before any planning logic runs; see
        # test_short_control_router_seam_fresh_process.py for the real end-to-end proof.
        source = inspect.getsource(control.execute_control_request)
        self.assertNotIn("production.install_router", source)
        self.assertIn("planning_runtime_contract.install_router", source)
        self.assertFalse(hasattr(control.production, "install_router"))

    def test_sibling_quota_cannot_escape_two_to_three(self):
        request = self._request(kind="long", scope="long_plus_sibling_shorts")
        request["sibling_shorts"] = {"minimum": 1, "maximum": 4}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "plan.json").write_text(
                json.dumps({"sections": [{"key_point": "أ"}, {"key_point": "ب"}, {"key_point": "ج"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "within 2–3"):
                control.write_sibling_short_plan(root, request)


if __name__ == "__main__":
    unittest.main()
