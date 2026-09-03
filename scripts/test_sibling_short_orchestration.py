from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import final_master_qc
from scripts import sibling_short_orchestration as sibling
from scripts.final_master_acceptance_v2 import seal_final_master_acceptance


def _editorial_intent() -> dict:
    return {
        "editorial_thesis": "التأجيل يتغذى على انتظار شعور كامل بالاستعداد قبل الحركة.",
        "viewer_starting_belief": "المشاهد يعتقد أن نقص الدافع هو السبب المباشر لعدم البدء.",
        "hidden_assumption": "الافتراض الخفي أن الثقة يجب أن تسبق أي خطوة عملية صغيرة.",
        "editorial_turn": "التحول أن الحركة الصغيرة يمكن أن تسبق الثقة وتبنيها تدريجيًا.",
        "stakes": "استمرار الانتظار يجعل المهام الصغيرة تبدو أكبر ويطيل دائرة الجمود.",
        "viewer_promise": "سيفهم المشاهد لماذا تكفي بداية صغيرة لكسر انتظار الاستعداد الكامل.",
        "evidence_boundaries": ["نلتزم بما تثبته الحلقة الأم ولا نضيف ادعاءات جديدة."],
        "earned_payoff": "يخرج المشاهد بخطوة واحدة صغيرة يبدأ بها اليوم بدل انتظار الدافع.",
        "persona_version": 1,
    }


class SiblingShortOrchestrationTests(unittest.TestCase):
    def _parent(self) -> dict:
        return {
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

    def _source_plan(self) -> dict:
        return {
            "topic": "موضوع الحلقة",
            "format": "film",
            "editorial_intent": _editorial_intent(),
            "sections": [
                {
                    "id": "s1",
                    "key_point": "الفكرة الأولى",
                    "on_screen_text": "أنت تؤجل البداية",
                    "visual_query": "person standing at starting line sunrise",
                    "emotion": "curious",
                    "narration": "أحيانًا لا يكون ما ينقصك هو الوقت. أنت تنتظر شعورًا كاملًا بالاستعداد قبل أن تبدأ. ابدأ بما تملكه الآن ثم صحح المسار وأنت تتحرك.",
                },
                {
                    "id": "s2",
                    "key_point": "الفكرة الثانية",
                    "on_screen_text": "الكمال يؤخر الحركة",
                    "visual_query": "hands erasing repeated notes desk",
                    "emotion": "reflective",
                    "narration": "قلت لنفسي إن كل شيء يجب أن يكون كاملًا. ثم سألت نفسي: ماذا لو كانت التجربة الصغيرة أوضح من التفكير وحده؟",
                },
                {
                    "id": "s3",
                    "key_point": "الفكرة الثالثة",
                    "on_screen_text": "خطوة صغيرة تغيّر المسار",
                    "visual_query": "single step on quiet road morning",
                    "emotion": "hopeful",
                    "narration": "الحركة الصغيرة لا تبدو بطولية لكنها تكسر الجمود. عندما تفعل شيئًا واضحًا اليوم يصبح الغد مبنيًا على دليل لا على أمنية. اجعل الخطوة قابلة للتكرار.",
                },
            ],
        }

    def _plan(self, jobs=None, *, source_plan_sha: str | None = None) -> dict:
        jobs = jobs or ["الفكرة الأولى", "الفكرة الثانية", "الفكرة الثالثة"]
        return {
            "schema_version": 1,
            "source_request_id": "req-parent",
            "source_request_sha256": "parent-sha",
            "source_production_plan_sha256": source_plan_sha or ("a" * 64),
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

    def _write_source_and_sibling_plan(self, root: Path, jobs=None) -> Path:
        source_plan = root / "plan.json"
        source_plan.write_text(json.dumps(self._source_plan(), ensure_ascii=False, indent=2), encoding="utf-8")
        plan_path = root / "sibling-short-plan.json"
        plan_path.write_text(
            json.dumps(self._plan(jobs, source_plan_sha=sibling._sha256_file(source_plan)), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return plan_path

    def _completed_output(self, root: Path, request: dict) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "final.mp4").write_bytes(b"v" * 4096)
        (root / "quality-final.json").write_text(
            json.dumps({"format": "moment", "duration_ok": True, "duration_seconds": 15.0}), encoding="utf-8"
        )
        (root / "short-intelligence.json").write_text(
            json.dumps({"request_id": request["request_id"], "delivery_allowed": True}), encoding="utf-8"
        )
        (root / "rights-manifest.json").write_text(json.dumps({"visuals": [{"id": "x"}]}), encoding="utf-8")
        (root / "plan.json").write_text(
            json.dumps({"topic": request["approved_topic"], "format": "moment"}, ensure_ascii=False), encoding="utf-8"
        )
        (root / "visual-timeline.json").write_text(
            json.dumps({"duration_seconds": 15.0}), encoding="utf-8"
        )
        sealed = seal_final_master_acceptance(
            root,
            {
                "schema_version": final_master_qc.SCHEMA_VERSION,
                "status": "pass",
                "production_stage": "post_render_pre_gold_acceptance",
                "full_decode_ok": True,
                "full_decode_timed_out": False,
                "final_media_mutated": False,
                "blocking_findings": [],
            },
            policy_fingerprint=final_master_qc.qc_policy_fingerprint(),
        )
        final_sha = sealed["acceptance_contract"]["sources"]["final"]["sha256"]
        (root / "gold-enforce-report.json").write_text(
            json.dumps(
                {
                    "phase": "4",
                    "mode": "enforce",
                    "gold": {"accepted": True},
                    "same_render": {
                        "artifact_divergence": False,
                        "sha256_before": final_sha,
                        "sha256_after": final_sha,
                    },
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_child_requests_inherit_parent_and_exact_source_section(self):
        requests = sibling.build_sibling_requests(self._parent(), self._plan(), self._source_plan())
        self.assertEqual(len(requests), 3)
        self.assertEqual([item["sibling_index"] for item in requests], [1, 2, 3])
        self.assertEqual(
            [item["source_short_plan"]["template"] for item in requests],
            ["why_reframe", "inner_dialogue", "micro_story"],
        )
        self.assertEqual(len({item["approved_topic"] for item in requests}), 3)
        for request in requests:
            self.assertTrue(request["approval_inherited_from_parent_bundle"])
            self.assertFalse(request["production_dispatch_authorized"])
            self.assertEqual(request["parent_control_request_id"], "req-parent")
            self.assertEqual(request["parent_control_request_sha256"], "parent-sha")
            self.assertEqual(request["source_production_plan_sha256"], "a" * 64)
            self.assertEqual(len(request["source_sibling_plan_sha256"]), 64)
            self.assertEqual(request["youtube_publish_mode"], "manual_in_youtube_studio")
            self.assertEqual(request["short_admission"]["evidence_source"], "approved_parent_candidate_metrics")
            self.assertEqual(
                request["source_editorial_intent"]["editorial_thesis"],
                _editorial_intent()["editorial_thesis"],
            )
            self.assertTrue(request["source_editorial_intent"]["editorial_fingerprint"])
            excerpt = request["source_episode_excerpt"]
            self.assertEqual(excerpt["source_key_point"], request["approved_topic"])
            self.assertTrue(excerpt["source_narration"])
            self.assertEqual(len(excerpt["source_narration_sha256"]), 64)
            self.assertEqual(request["source_short_plan"]["source_kind"], "long_episode")
            self.assertEqual(request["source_short_plan"]["semantic_job"], request["approved_topic"])
            self.assertGreaterEqual(len(request["source_short_plan"]["beats"]), 2)
            self.assertEqual(request["request_sha256"], sibling._canonical_hash({k: v for k, v in request.items() if k != "request_sha256"}))

    def test_source_long_editorial_intent_is_required(self):
        source_plan = self._source_plan()
        source_plan.pop("editorial_intent")
        with self.assertRaisesRegex(RuntimeError, "source long EditorialIntent"):
            sibling.build_sibling_requests(self._parent(), self._plan(), source_plan)

    def test_parent_candidate_must_pass_short_topic_admission(self):
        parent = self._parent()
        for key in ("hook_potential", "retention_potential", "emotional_pull", "audience_fit", "title_thumbnail_potential", "production_feasibility"):
            parent["candidate"][key] = 0.2
        with self.assertRaisesRegex(RuntimeError, "not strong enough"):
            sibling.build_sibling_requests(parent, self._plan(), self._source_plan())

    def test_plan_must_have_two_to_three_distinct_jobs(self):
        with self.assertRaisesRegex(RuntimeError, "2–3"):
            sibling.build_sibling_requests(self._parent(), self._plan(["واحدة"]), self._source_plan())
        with self.assertRaisesRegex(RuntimeError, "distinct"):
            sibling.build_sibling_requests(self._parent(), self._plan(["نفس الفكرة", "نفس الفكرة"]), self._source_plan())

    def test_sibling_plan_requires_parent_request_and_long_plan_hashes(self):
        plan = self._plan()
        plan["source_request_sha256"] = "other"
        with self.assertRaisesRegex(RuntimeError, "parent request hash mismatch"):
            sibling.build_sibling_requests(self._parent(), plan, self._source_plan())
        plan = self._plan()
        plan["source_production_plan_sha256"] = "bad"
        with self.assertRaisesRegex(RuntimeError, "production-plan hash"):
            sibling.build_sibling_requests(self._parent(), plan, self._source_plan())

    def test_job_must_map_to_exactly_one_real_long_section(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            sibling.build_sibling_requests(self._parent(), self._plan(["غير موجود", "الفكرة الثانية"]), self._source_plan())

    def test_orchestration_executes_sequentially_and_validates_all_children(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_path = self._write_source_and_sibling_plan(root)
            calls = []

            def execute(request):
                calls.append(request["request_id"])
                return self._completed_output(root / request["request_id"], request)

            completed = sibling.orchestrate_sibling_shorts(self._parent(), plan_path, execute_short=execute)
            self.assertEqual(calls, ["req-parent-s1", "req-parent-s2", "req-parent-s3"])
            self.assertEqual(len(completed), 3)
            self.assertTrue(all(item["delivery_allowed"] for item in completed))
            self.assertEqual([item["source_section_id"] for item in completed], ["s1", "s2", "s3"])
            self.assertTrue(all(Path(item["final_master_qc"]).name == "final-master-qc.json" for item in completed))

    def test_orchestration_blocks_if_long_plan_changes_after_sibling_planning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_path = self._write_source_and_sibling_plan(root)
            (root / "plan.json").write_text(json.dumps({"tampered": True}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "source production plan changed"):
                sibling.orchestrate_sibling_shorts(self._parent(), plan_path, execute_short=lambda _: root / "unused")

    def test_orchestration_stops_on_first_failed_child_and_never_claims_partial_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_path = self._write_source_and_sibling_plan(root)
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
                    "source_episode_excerpt": {"source_section_id": f"s{index}"},
                    "source_production_plan_sha256": "a" * 64,
                    "source_sibling_plan_sha256": "b" * 64,
                }
                output = self._completed_output(root / f"child-{index}", request)
                completed.append(sibling.validate_completed_short(output, request))
            staged = sibling.stage_sibling_assets(parent, completed)
            self.assertEqual([item["video"] for item in staged], ["short-01.mp4", "short-02.mp4"])
            self.assertTrue((parent / "short-01.mp4").is_file())
            self.assertTrue((parent / "short-02-intelligence.json").is_file())
            self.assertTrue((parent / "short-01-master-qc.json").is_file())
            self.assertTrue((parent / "short-02-master-qc.json").is_file())
            self.assertEqual([item["source_section_id"] for item in staged], ["s1", "s2"])
            self.assertTrue(all(len(item["final_sha256"]) == 64 for item in staged))


if __name__ == "__main__":
    unittest.main()
