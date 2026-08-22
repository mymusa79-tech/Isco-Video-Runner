from __future__ import annotations

import hashlib
import unittest

import isco_video_agent.orchestrator as orchestrator
from scripts import source_derived_short_planner as planner


class SourceDerivedShortPlannerTests(unittest.TestCase):
    def _request(self) -> dict:
        narration = (
            "أحيانًا لا يكون ما ينقصك هو الوقت. "
            "أنت تنتظر شعورًا كاملًا بالاستعداد قبل أن تبدأ. "
            "ابدأ بما تملكه الآن ثم صحح المسار وأنت تتحرك."
        )
        request = {
            "kind": "short",
            "approval_scope": "short_sibling",
            "approval_inherited_from_parent_bundle": True,
            "production_dispatch_authorized": False,
            "approved_topic": "الفكرة الأولى",
            "parent_control_request_id": "req-parent",
            "source_production_plan_sha256": "a" * 64,
            "source_semantic_job": "الفكرة الأولى",
            "candidate": {"pillar": "rise"},
            "short_admission": {"single_action_contract": "اختر خطوة واحدة صغيرة قابلة للتنفيذ وابدأ بها اليوم"},
            "source_episode_excerpt": {
                "source_section_id": "s1",
                "source_key_point": "الفكرة الأولى",
                "source_on_screen_text": "أنت تؤجل البداية",
                "source_visual_query": "person standing at starting line sunrise",
                "source_emotion": "curious",
                "source_narration": narration,
                "source_narration_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
            },
        }
        request["source_short_plan"] = planner.build_source_short_blueprint(request)
        return request

    def test_blueprint_uses_engine_native_short_plan_and_long_episode_source(self):
        request = self._request()
        blueprint = request["source_short_plan"]
        self.assertEqual(blueprint["planner"], "native_short_planner")
        self.assertEqual(blueprint["source_kind"], "long_episode")
        self.assertEqual(blueprint["semantic_job"], "الفكرة الأولى")
        self.assertGreaterEqual(len(blueprint["beats"]), 2)
        self.assertLessEqual(len(blueprint["beats"]), 4)

    def test_production_plan_is_moment_and_uses_exact_source_visual(self):
        plan = planner.build_production_plan(self._request())
        self.assertEqual(plan.format, "moment")
        self.assertEqual(plan.topic, "الفكرة الأولى")
        self.assertEqual(len(plan.sections), 1)
        self.assertEqual(plan.sections[0].visual_query, "person standing at starting line sunrise")
        self.assertEqual(plan.sections[0].narration, "")
        self.assertTrue(plan.hook)
        self.assertTrue(plan.closing_payoff)
        self.assertNotEqual(plan.hook, plan.closing_payoff)

    def test_source_narration_tampering_is_blocked(self):
        request = self._request()
        request["source_episode_excerpt"]["source_narration"] += " تغيير"
        with self.assertRaisesRegex(planner.SourceDerivedShortError, "source_narration_integrity_failed"):
            planner.build_production_plan(request)

    def test_stored_blueprint_tampering_is_blocked(self):
        request = self._request()
        request["source_short_plan"]["semantic_job"] = "فكرة أخرى"
        with self.assertRaisesRegex(planner.SourceDerivedShortError, "blueprint_changed"):
            planner.build_production_plan(request)

    def test_installed_planner_satisfies_production_router_marker_and_rejects_topic_change(self):
        original = orchestrator.build_plan
        try:
            request = self._request()
            planner.install_source_derived_short_planner(request)
            self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))
            plan = orchestrator.build_plan(None, "الفكرة الأولى", "moment", "ignored")
            self.assertEqual(plan.format, "moment")
            with self.assertRaisesRegex(planner.SourceDerivedShortError, "topic_changed"):
                orchestrator.build_plan(None, "موضوع آخر", "moment", "ignored")
            with self.assertRaisesRegex(planner.SourceDerivedShortError, "format_must_be_moment"):
                orchestrator.build_plan(None, "الفكرة الأولى", "film", "ignored")
        finally:
            orchestrator.build_plan = original


if __name__ == "__main__":
    unittest.main()
