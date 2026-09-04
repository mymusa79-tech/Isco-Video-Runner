from __future__ import annotations

import hashlib
import unittest

from scripts import source_derived_short_planner as planner
from scripts.voice_owned_timeline import provision_source_derived_visual_seconds


def _editorial_intent() -> dict:
    return {
        "editorial_thesis": "البدء الصغير يقطع انتظار الاستعداد الكامل ويعيد الحركة إلى اليوم.",
        "viewer_starting_belief": "المشاهد يعتقد أنه يحتاج شعورًا كاملًا بالثقة قبل أن يبدأ.",
        "hidden_assumption": "الافتراض الخفي أن الثقة يجب أن تسبق الحركة بدل أن تنمو بعدها.",
        "editorial_turn": "التحول أن خطوة صغيرة يمكن أن تسبق الثقة وتبنيها تدريجيًا.",
        "stakes": "استمرار الانتظار يجعل المهمة أكبر ويطيل دائرة الجمود من دون داعٍ.",
        "viewer_promise": "سيفهم المشاهد لماذا تكفي بداية صغيرة لكسر انتظار الاستعداد الكامل.",
        "evidence_boundaries": ["نلتزم بما تثبته الحلقة الأم ولا نضيف ادعاءات جديدة."],
        "earned_payoff": "يخرج المشاهد بخطوة واحدة صغيرة يبدأ بها اليوم بدل انتظار الدافع.",
        "persona_version": 1,
    }


def _request() -> dict:
    semantic_job = "لماذا انتظار الثقة الكاملة قبل البداية يطيل الجمود ويجعل الخطوة الأولى أصعب"
    on_screen = "لا تنتظر أن يختفي الخوف كله قبل أن تتحرك خطوة صغيرة واضحة اليوم"
    narration = (
        "قد تبقى طويلًا أمام المهمة لأنك تنتظر شعورًا كاملًا بالأمان والثقة قبل الحركة الأولى. "
        "لكن البداية الصغيرة لا تحتاج اختفاء القلق كله بل تحتاج مساحة تكفي لخطوة واحدة صادقة."
    )
    request = {
        "kind": "short",
        "approval_scope": "short_sibling",
        "approval_inherited_from_parent_bundle": True,
        "production_dispatch_authorized": False,
        "approved_topic": semantic_job,
        "parent_control_request_id": "req-parent-voice-owned",
        "source_production_plan_sha256": "a" * 64,
        "source_semantic_job": semantic_job,
        "source_editorial_intent": _editorial_intent(),
        "candidate": {"pillar": "rise"},
        "short_admission": {
            "single_action_contract": "اختر خطوة واحدة صغيرة قابلة للتنفيذ وابدأ بها اليوم"
        },
        "source_episode_excerpt": {
            "source_section_id": "s1",
            "source_key_point": semantic_job,
            "source_on_screen_text": on_screen,
            "source_visual_query": "person pausing before first step sunrise realistic",
            "source_emotion": "restrained anxiety to calm resolve",
            "source_narration": narration,
            "source_narration_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
        },
    }
    request["source_short_plan"] = planner.build_source_short_blueprint(request)
    return request


class SourceDerivedVoiceOwnedBudgetTests(unittest.TestCase):
    def test_source_derived_visual_budget_follows_approved_beats_not_fixed_15_seconds(self):
        request = _request()
        blueprint = request["source_short_plan"]
        beat_texts = [str(item["text"]) for item in blueprint["beats"]]
        expected = provision_source_derived_visual_seconds(beat_texts)
        plan = planner.build_production_plan(request)

        self.assertEqual(plan.sections[0].expected_seconds, expected)
        self.assertNotEqual(plan.sections[0].expected_seconds, 15.0)
        self.assertGreaterEqual(plan.sections[0].expected_seconds, 12.0)
        self.assertLessEqual(plan.sections[0].expected_seconds, 24.5)
        self.assertEqual(plan.sections[0].narration, "")
        self.assertEqual(
            plan.sections[0].visual_query,
            request["source_episode_excerpt"]["source_visual_query"],
        )

    def test_visual_budget_is_provisioning_only_and_never_changes_source_text(self):
        request = _request()
        before = request["source_episode_excerpt"]["source_narration"]
        planner.build_production_plan(request)
        self.assertEqual(request["source_episode_excerpt"]["source_narration"], before)
        self.assertEqual(
            hashlib.sha256(before.encode("utf-8")).hexdigest(),
            request["source_episode_excerpt"]["source_narration_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
