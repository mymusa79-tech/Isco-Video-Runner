from __future__ import annotations

import hashlib
import unittest

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.short_hook_director import HOOK_DEADLINE_MS, validate_hook_schema
from isco_video_agent.short_planner import ALLOWED_TEMPLATES, validate_single_action_contract
from scripts import source_derived_short_planner as planner


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


class SourceDerivedShortPlannerTests(unittest.TestCase):
    def _request(
        self,
        *,
        pillar: str = "rise",
        sibling_index: object | None = None,
        semantic_job: str = "الفكرة الأولى",
        on_screen_text: str = "أنت تؤجل البداية",
        narration: str | None = None,
    ) -> dict:
        narration = narration or (
            "أحيانًا لا يكون ما ينقصك هو الوقت. "
            "أنت تنتظر شعورًا كاملًا بالاستعداد قبل أن تبدأ. "
            "ابدأ بما تملكه الآن ثم صحح المسار وأنت تتحرك."
        )
        request = {
            "kind": "short",
            "approval_scope": "short_sibling",
            "approval_inherited_from_parent_bundle": True,
            "production_dispatch_authorized": False,
            "approved_topic": semantic_job,
            "parent_control_request_id": "req-parent",
            "source_production_plan_sha256": "a" * 64,
            "source_semantic_job": semantic_job,
            "source_editorial_intent": _editorial_intent(),
            "candidate": {"pillar": pillar},
            "short_admission": {"single_action_contract": "اختر خطوة واحدة صغيرة قابلة للتنفيذ وابدأ بها اليوم"},
            "source_episode_excerpt": {
                "source_section_id": "s1",
                "source_key_point": semantic_job,
                "source_on_screen_text": on_screen_text,
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

    def _template_cases(self) -> tuple[dict, ...]:
        return (
            {
                "expected": "why_reframe",
                "semantic_job": "لماذا فقدان الدافع لا يعني أنك كسول",
                "on_screen_text": "المشكلة ليست في شخصيتك",
                "narration": (
                    "قد تظن أن فقدان الدافع يعني أنك كسول. "
                    "لكن الحقيقة أن المشكلة ليست في شخصيتك بل في طريقة استعادة الطاقة."
                ),
            },
            {
                "expected": "micro_story",
                "semantic_job": "اليوم الذي بدأت فيه من جديد",
                "on_screen_text": "خطوة واحدة غيرت اليوم",
                "narration": (
                    "ذات يوم بقيت أمام المهمة ساعات. "
                    "ثم قررت تنفيذ خطوة صغيرة وبعد ذلك تغير مساري."
                ),
            },
            {
                "expected": "inner_dialogue",
                "semantic_job": "الصوت الداخلي الذي يخاف البداية",
                "on_screen_text": "ماذا لو فشلت؟",
                "narration": (
                    "قلت لنفسي: لن أستطيع البدء. "
                    "ثم سألت نفسي: ماذا لو بدأت لدقيقتين فقط؟"
                ),
            },
            {
                "expected": "quote_reflection",
                "semantic_job": "تأمل العبارة التي غيرت قرارك",
                "on_screen_text": "«ابدأ قبل أن تشعر بالاستعداد»",
                "narration": (
                    "هذه العبارة موجودة في الحلقة: «ابدأ قبل أن تشعر بالاستعداد». "
                    "معناها أن الحركة تسبق الثقة."
                ),
            },
        )

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

    def test_all_four_engine_templates_are_selected_from_source_topic(self):
        selected = set()
        for sibling_index, case in enumerate(self._template_cases(), 1):
            expected = case["expected"]
            with self.subTest(expected=expected):
                blueprint = self._request(
                    pillar="unknown",
                    sibling_index=sibling_index,
                    semantic_job=case["semantic_job"],
                    on_screen_text=case["on_screen_text"],
                    narration=case["narration"],
                )["source_short_plan"]
                self.assertEqual(blueprint["template"], expected)
                selected.add(blueprint["template"])
        self.assertEqual(selected, ALLOWED_TEMPLATES)

    def test_topic_fit_wins_over_every_sibling_slot(self):
        for case in self._template_cases():
            for sibling_index in (1, 2, 3):
                with self.subTest(expected=case["expected"], sibling_index=sibling_index):
                    request = self._request(
                        pillar="unknown",
                        sibling_index=sibling_index,
                        semantic_job=case["semantic_job"],
                        on_screen_text=case["on_screen_text"],
                        narration=case["narration"],
                    )
                    self.assertEqual(request["source_short_plan"]["template"], case["expected"])

    def test_sibling_slot_only_breaks_equal_topic_scores(self):
        kwargs = {
            "pillar": "unknown",
            "semantic_job": "فكرة محايدة",
            "on_screen_text": "نقطة واضحة",
            "narration": "يوضح هذا القسم معنى واضحًا. يقدم تفصيلًا عمليًا مستقلًا.",
        }
        self.assertEqual(
            self._request(sibling_index=1, **kwargs)["source_short_plan"]["template"],
            "why_reframe",
        )
        self.assertEqual(
            self._request(sibling_index="2", **kwargs)["source_short_plan"]["template"],
            "inner_dialogue",
        )
        self.assertEqual(
            self._request(sibling_index=3, **kwargs)["source_short_plan"]["template"],
            "micro_story",
        )

    def test_quote_reflection_requires_real_source_quote_evidence(self):
        request = self._request(
            pillar="unknown",
            sibling_index=3,
            semantic_job="فكرة محايدة",
            on_screen_text="نقطة واضحة",
            narration="يوضح القسم معنى واضحًا. يقدم تفصيلًا عمليًا مستقلًا.",
        )
        self.assertEqual(request["source_short_plan"]["template"], "micro_story")
        self.assertLess(planner._template_scores(request)["quote_reflection"], 0)

    def test_contextual_selection_is_deterministic_when_blueprint_is_recomputed(self):
        case = self._template_cases()[2]
        request = self._request(
            sibling_index=3,
            semantic_job=case["semantic_job"],
            on_screen_text=case["on_screen_text"],
            narration=case["narration"],
        )
        self.assertEqual(planner.build_source_short_blueprint(request), request["source_short_plan"])

    def test_every_template_keeps_hook_and_single_action_contracts(self):
        for sibling_index, case in enumerate(self._template_cases(), 1):
            with self.subTest(expected=case["expected"]):
                request = self._request(
                    pillar="unknown",
                    sibling_index=sibling_index,
                    semantic_job=case["semantic_job"],
                    on_screen_text=case["on_screen_text"],
                    narration=case["narration"],
                )
                blueprint = request["source_short_plan"]
                hook = self._hook_contract(blueprint)
                self.assertLessEqual(hook["hook_commit_ms"], HOOK_DEADLINE_MS)
                self.assertEqual(hook["hook_commit_ms"], 0)
                self.assertEqual(
                    validate_single_action_contract(blueprint["single_action_contract"]),
                    request["short_admission"]["single_action_contract"],
                )

    def test_contextual_template_tampering_fails_closed(self):
        case = self._template_cases()[2]
        request = self._request(
            sibling_index=2,
            semantic_job=case["semantic_job"],
            on_screen_text=case["on_screen_text"],
            narration=case["narration"],
        )
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
        self.assertEqual(
            plan.editorial_intent["editorial_thesis"],
            _editorial_intent()["editorial_thesis"],
        )
        self.assertEqual(plan.editorial_intent["persona_version"], 1)
        self.assertTrue(plan.editorial_intent["editorial_fingerprint"])

    def test_missing_inherited_editorial_intent_fails_closed(self):
        request = self._request()
        request.pop("source_editorial_intent")
        with self.assertRaisesRegex(planner.SourceDerivedShortError, "source_editorial_intent_missing"):
            planner.build_production_plan(request)

    def test_invalid_inherited_editorial_intent_fails_closed(self):
        request = self._request()
        request["source_editorial_intent"]["persona_version"] = 999
        with self.assertRaisesRegex(planner.SourceDerivedShortError, "source_editorial_intent_invalid"):
            planner.build_production_plan(request)

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
