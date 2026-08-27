from __future__ import annotations

import hashlib
import unittest

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.short_hook_director import HOOK_DEADLINE_MS, validate_hook_schema
from isco_video_agent.short_planner import ALLOWED_TEMPLATES, validate_single_action_contract
from scripts import source_derived_short_planner as planner


class SourceDerivedShortPlannerTests(unittest.TestCase):
    def _request(self, *, pillar: str = "rise", sibling_index: object | None = None) -> dict:
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
            "candidate": {"pillar": pillar},
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
        if sibling_index is not None:
            request["sibling_index"] = sibling_index
        request["source_short_plan"] = planner.build_source_short_blueprint(request)
        return request

    def _hook_contract(self, blueprint: dict) -> dict:
        beats = []
        for index, beat in enumerate(blueprint["beats"]):
            beats.append(
                {
                    "beat_id": beat["beat_id"],
                    "start_ms": index * 1000,
                    "end_ms": (index + 1) * 1000,
                    "semantic_job": beat["semantic_job"],
                    "hook_commit": index == 0,
                }
            )
        return validate_hook_schema({"hook_commit_ms": 0, "beats": beats})

    def test_blueprint_uses_engine_native_short_plan_and_long_episode_source(self):
        request = self._request()
        blueprint = request["source_short_plan"]
        self.assertEqual(blueprint["planner"], "native_short_planner")
        self.assertEqual(blueprint["source_kind"], "long_episode")
        self.assertEqual(blueprint["semantic_job"], "الفكرة الأولى")
        self.assertGreaterEqual(len(blueprint["beats"]), 2)
        self.assertLessEqual(len(blueprint["beats"]), 4)

    def test_all_four_engine_templates_are_selectable(self):
        cases = (
            ("rise", 1, "why_reframe"),
            ("see", 1, "micro_story"),
            ("rise", 2, "inner_dialogue"),
            ("see", 3, "quote_reflection"),
        )
        selected = set()
        for pillar, sibling_index, expected in cases:
            with self.subTest(pillar=pillar, sibling_index=sibling_index):
                blueprint = self._request(pillar=pillar, sibling_index=sibling_index)["source_short_plan"]
                self.assertEqual(blueprint["template"], expected)
                selected.add(blueprint["template"])
        self.assertEqual(selected, ALLOWED_TEMPLATES)

    def test_first_sibling_and_missing_index_preserve_legacy_pillar_mapping(self):
        for sibling_index in (None, 1, 0, 4, "invalid", True):
            with self.subTest(sibling_index=sibling_index):
                self.assertEqual(
                    self._request(pillar="see", sibling_index=sibling_index)["source_short_plan"]["template"],
                    "micro_story",
                )
                self.assertEqual(
                    self._request(pillar="rise", sibling_index=sibling_index)["source_short_plan"]["template"],
                    "why_reframe",
                )

    def test_numeric_string_sibling_slots_select_new_templates_deterministically(self):
        self.assertEqual(
            self._request(sibling_index="2")["source_short_plan"]["template"],
            "inner_dialogue",
        )
        self.assertEqual(
            self._request(sibling_index="3")["source_short_plan"]["template"],
            "quote_reflection",
        )
        request = self._request(sibling_index=3)
        self.assertEqual(planner.build_source_short_blueprint(request), request["source_short_plan"])

    def test_every_template_keeps_hook_and_single_action_contracts(self):
        cases = (("rise", 1), ("see", 1), ("rise", 2), ("rise", 3))
        for pillar, sibling_index in cases:
            with self.subTest(pillar=pillar, sibling_index=sibling_index):
                request = self._request(pillar=pillar, sibling_index=sibling_index)
                blueprint = request["source_short_plan"]
                hook = self._hook_contract(blueprint)
                self.assertLessEqual(hook["hook_commit_ms"], HOOK_DEADLINE_MS)
                self.assertEqual(hook["hook_commit_ms"], 0)
                self.assertEqual(
                    validate_single_action_contract(blueprint["single_action_contract"]),
                    request["short_admission"]["single_action_contract"],
                )

    def test_template_selection_changes_no_other_blueprint_contract_field(self):
        blueprints = [
            self._request(pillar="rise", sibling_index=index)["source_short_plan"]
            for index in (1, 2, 3)
        ]
        without_template = [
            {key: value for key, value in item.items() if key != "template"}
            for item in blueprints
        ]
        self.assertEqual(without_template[0], without_template[1])
        self.assertEqual(without_template[1], without_template[2])

    def test_pre_change_binary_blueprint_for_new_slot_fails_closed(self):
        request = self._request(sibling_index=2)
        request["source_short_plan"]["template"] = "why_reframe"
        with self.assertRaisesRegex(planner.SourceDerivedShortError, "blueprint_changed"):
            planner.build_production_plan(request)

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
