from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import sibling_short_orchestration as sibling


class SiblingShortOrchestrationTests(unittest.TestCase):
    def _parent(self) -> dict:
        request = {
            "schema_version": 1,
            "request_id": "req-parent",
            "request_sha256": "parent-sha",
            "source": "telegram_editorial_control_panel",
            "kind": "long",
            "approval_scope": "long_plus_sibling_shorts",
            "approved_by_user": True,
            "approved_at": "2026-08-22T12:00:00+00:00",
            "approved_topic": "موضوع الحلقة",
            "weekly_option_id": "telegram:s:1",
            "content_boundaries": [],
            "production_dispatch_authorized": False,
            "status": "approved_waiting_production_activation",
            "candidate": {
                "pillar": "rise",
                "control_score": 0.90,
                "hook_potential": 0.90,
                "retention_potential": 0.86,
                "emotional_pull": 0.82,
                "audience_fit": 0.90,
                "title_thumbnail_potential": 0.82,
                "production_feasibility": 0.88,
                "evidence_quality": 0.75,
            },
        }
        return request

    def _plan(self, jobs=None) -> dict:
        jobs = jobs or ["الفكرة الأولى", "الفكرة الثانية", "الفكرة الثالثة"]
        return {
            "schema_version": 1,
            "source_request_id": "req-parent",
            "source_request_sha256": "parent-sha",
            "source_topic": "موضوع الحلقة",
            "short_count": len(jobs),
            "semantic_jobs": [
                {
                    "index": index,
                    "semantic_job": job,
                    "status": "planned_not_dispatched",
                    "production_dispatch_authorized": False,
                }
                for index, job in enumerate(jobs, 1)
            ],
            "automatic_production_started": False,
        }

    def _completed_output(self, root: Path, request: dict) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "final.mp4").write_bytes(b"v" * 4096)
        (root / "quality-final.json").write_text(
            json.dumps({"format": "moment", "duration_ok": True, "duration_seconds": 15.0}), encoding="utf-8"
        )
        (root / "short-intelligence.json").write_text(
            json.dumps({"request_id": request["request_id"], "delivery_allowed": True}), encoding="utf-8"
        )
        (root / "gold-enforce-report.json").write_text(
            json.dumps({"phase": "4", "mode": "enforce", "gold": {"accepted": True}, "same_render": {"artifact_divergence": False}}),
            encoding="utf-8",
        )
        (root / "rights-manifest.json").write_text(json.dumps({"visuals": [{"id": "x"}]}), encoding="utf-8")
        (root / "plan.json").write_text(
            json.dumps({"topic": request["approved_topic"], "format": "moment"}, ensure_ascii=False), encoding="utf-8"
        )
        return root

    def test_child_requests_inherit_parent_bundle_without_persisted_dispatch_right(self):
        requests = sibling.build_sibling_requests(self._parent(), self._plan())
        self.assertEqual(len(requests), 3)
        self.assertEqual([item["sibling_index"] for item in requests], [1, 2, 3])
        self.assertEqual(len({item["approved_topic"] for item in requests}), 3)
        for request in requests:
            self.assertTrue(request["approval_inherited_from_parent_bundle"])
            self.assertFalse(request["production_dispatch_authorized"])
            self.assertEqual(request["parent_control_request_id"], "req-parent")
            self.assertEqual(request["parent_control_request_sha256"], "parent-sha")
            self.assertEqual(request["youtube_publish_mode"], "manual_in_youtube_studio")
            self.assertEqual(request["short_admission"]["evidence_source"], "approved_parent_candidate_metrics")
            self.assertEqual(request["request_sha256"], sibling._canonical_hash({k: v for k, v in request.items() if k != "request_sha256"}))

    def test_parent_candidate_must_pass_short_topic_admission(self):
        parent = self._parent()
        for key in ("hook_potential", "retention_potential", "emotional_pull", "audience_fit", "title_thumbnail_potential", "production_feasibility"):
            parent["candidate"][key] = 0.2
        with self.assertRaisesRegex(RuntimeError, "not strong enough"):
            sibling.build_sibling_requests(parent, self._plan())

    def test_plan_must_have_two_to_three_distinct_jobs(self):
        with self.assertRaisesRegex(RuntimeError, "2–3"):
            sibling.build_sibling_requests(self._parent(), self._plan(["واحدة"]))
        plan = self._plan(["نفس الفكرة", "نفس الفكرة"])
        with self.assertRaisesRegex(RuntimeError, "distinct"):
            sibling.build_sibling_requests(self._parent(), plan)

    def test_orchestration_executes_sequentially_and_validates_all_children(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_path = root / "sibling-short-plan.json"
            plan_path.write_text(json.dumps(self._plan(), ensure_ascii=False), encoding="utf-8")
            calls = []

            def execute(request):
                calls.append(request["request_id"])
                return self._completed_output(root / request["request_id"], request)

            completed = sibling.orchestrate_sibling_shorts(self._parent(), plan_path, execute_short=execute)
            self.assertEqual(calls, ["req-parent-s1", "req-parent-s2", "req-parent-s3"])
            self.assertEqual(len(completed), 3)
            self.assertTrue(all(item["delivery_allowed"] for item in completed))

    def test_orchestration_stops_on_first_failed_child_and_never_claims_partial_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_path = root / "sibling-short-plan.json"
            plan_path.write_text(json.dumps(self._plan(), ensure_ascii=False), encoding="utf-8")
            calls = []

            def execute(request):
                calls.append(request["request_id"])
                output = self._completed_output(root / request["request_id"], request)
                if request["sibling_index"] == 2:
                    (output / "short-intelligence.json").write_text(
                        json.dumps({"request_id": request["request_id"], "delivery_allowed": False}), encoding="utf-8"
                    )
                return output

            with self.assertRaisesRegex(RuntimeError, "did not allow delivery"):
                sibling.orchestrate_sibling_shorts(self._parent(), plan_path, execute_short=execute)
            self.assertEqual(calls, ["req-parent-s1", "req-parent-s2"])

    def test_staging_flattens_unique_assets_for_one_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "parent"
            parent.mkdir()
            completed = []
            for index, job in enumerate(("أ", "ب"), 1):
                request = {
                    "request_id": f"req-parent-s{index}",
                    "request_sha256": f"sha-{index}",
                    "approved_topic": job,
                    "source_semantic_job": job,
                }
                output = self._completed_output(root / f"child-{index}", request)
                completed.append(sibling.validate_completed_short(output, request))
            staged = sibling.stage_sibling_assets(parent, completed)
            self.assertEqual([item["video"] for item in staged], ["short-01.mp4", "short-02.mp4"])
            self.assertTrue((parent / "short-01.mp4").is_file())
            self.assertTrue((parent / "short-02-intelligence.json").is_file())


if __name__ == "__main__":
    unittest.main()
