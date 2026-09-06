from __future__ import annotations

import hashlib
import inspect
import unittest

from scripts import orchestration_shorts_port as port
from scripts import producer_quality_contract
from scripts import short_editorial_craft_contract as craft
from scripts import short_voice_owned_timeline as runtime
from scripts.source_derived_short_planner import _distinct_source_texts
from scripts.voice_owned_timeline import (
    VoiceOwnedTimelineError,
    build_voice_owned_timeline,
    provision_source_derived_visual_seconds,
    retime_events,
)


class VoiceOwnedTimelineV1Tests(unittest.TestCase):
    def test_run196_shape_extends_visual_instead_of_speeding_voice(self):
        contract = build_voice_owned_timeline(
            voice_seconds=17.70,
            source_visual_seconds=14.00,
            minimum_seconds=7.0,
            maximum_seconds=25.0,
            mode="voice_led",
            visible_beat_count=4,
            source_derived_from_long=False,
        )
        self.assertAlmostEqual(contract["target_seconds"], 18.05, places=2)
        self.assertEqual(contract["post_speed_factor"], 1.0)
        self.assertFalse(contract["time_compression"])
        self.assertEqual(contract["timeline_owner"], "voice")
        self.assertGreater(contract["timeline_adjustment_seconds"], 4.0)

    def test_runtime_contains_no_atempo_voice_fit(self):
        source = inspect.getsource(runtime)
        self.assertNotIn("atempo=", source)
        self.assertNotIn("_fit_voice_to_video", source)
        self.assertIn('"voice_post_speed_factor": 1.0', source)
        self.assertIn('"voice_time_compression": False', source)
        self.assertEqual(runtime.VOICE_TASK_ID, "SHORT_VOICE_V2")

    def test_performance_script_preserves_words_and_adds_breathing_punctuation(self):
        events = [
            {"text": "أنا خائف؟"},
            {"text": "لكنني ما زلت هنا"},
            {"text": "يمكنني أن أبدأ بهدوء"},
        ]
        script = runtime._performance_script(events, "voice_led", "inner_dialogue")
        self.assertEqual(script, "أنا خائف… لكنني ما زلت هنا… يمكنني أن أبدأ بهدوء.")
        for expected in ("أنا خائف", "لكنني ما زلت هنا", "يمكنني أن أبدأ بهدوء"):
            self.assertIn(expected, script)

    def test_hybrid_performance_speaks_hook_and_payoff_with_pause(self):
        events = [
            {"text": "الفكرة الأولى"},
            {"text": "معلومة بصرية"},
            {"text": "الخلاصة الأخيرة"},
        ]
        script = runtime._performance_script(events, "hybrid", "quote_reflection")
        self.assertEqual(script, "الفكرة الأولى… الخلاصة الأخيرة.")
        self.assertNotIn("معلومة بصرية", script)

    def test_hard_max_requests_planning_repair_instead_of_speed(self):
        with self.assertRaisesRegex(VoiceOwnedTimelineError, "planning_repair_required=true"):
            build_voice_owned_timeline(
                voice_seconds=25.10,
                source_visual_seconds=14.0,
                minimum_seconds=7.0,
                maximum_seconds=25.0,
                mode="voice_led",
                visible_beat_count=4,
                source_derived_from_long=False,
            )

    def test_hybrid_keeps_visible_beat_readability_floor(self):
        contract = build_voice_owned_timeline(
            voice_seconds=4.0,
            source_visual_seconds=14.0,
            minimum_seconds=7.0,
            maximum_seconds=25.0,
            mode="hybrid",
            visible_beat_count=5,
            source_derived_from_long=False,
        )
        self.assertAlmostEqual(contract["target_seconds"], 9.0, places=2)
        self.assertEqual(contract["timeline_owner"], "voice_plus_visible_beats")
        self.assertEqual(contract["post_speed_factor"], 1.0)

    def test_events_follow_measured_timeline_without_reordering(self):
        events = [
            {"role": "hook", "text": "a", "start": 0.0, "end": 3.5},
            {"role": "development", "text": "b", "start": 3.5, "end": 8.0},
            {"role": "payoff", "text": "c", "start": 8.0, "end": 14.0},
        ]
        retimed = retime_events(events, source_seconds=14.0, target_seconds=18.05)
        self.assertEqual([x["role"] for x in retimed], ["hook", "development", "payoff"])
        self.assertEqual(retimed[0]["start"], 0.0)
        self.assertAlmostEqual(retimed[-1]["end"], 18.05, places=2)
        self.assertLess(retimed[0]["end"], retimed[1]["end"])

    def test_template_aware_hook_window_caps_run219_like_voice_extension(self):
        events = [
            {"role": "hook", "text": "hook", "start": 0.0, "end": 2.8},
            {"role": "development", "text": "development", "start": 2.8, "end": 7.0},
            {"role": "turn", "text": "turn", "start": 7.0, "end": 11.0},
            {"role": "payoff", "text": "payoff", "start": 11.0, "end": 14.0},
        ]
        expected_caps = {
            "why_reframe": 3.0,
            "inner_dialogue": 3.2,
            "micro_story": 3.3,
            "quote_reflection": 3.5,
        }
        self.assertEqual(craft.template_names(), tuple(expected_caps))
        for template, cap in expected_caps.items():
            with self.subTest(template=template):
                self.assertEqual(craft.template_hook_beat_max_seconds(template), cap)
                retimed = retime_events(
                    events,
                    source_seconds=14.0,
                    target_seconds=20.97,
                    first_event_max_seconds=cap,
                )
                self.assertLessEqual(float(retimed[0]["end"]), cap + 0.001)
                self.assertAlmostEqual(float(retimed[-1]["end"]), 20.97, places=2)
                self.assertEqual([item["text"] for item in retimed], [item["text"] for item in events])
                self.assertEqual([item["role"] for item in retimed], [item["role"] for item in events])

    def test_existing_producer_call_receives_four_template_and_long_craft_guidance(self):
        directive = producer_quality_contract.producer_writing_directive({})
        for template in craft.template_names():
            self.assertIn(template, directive)
        self.assertIn("Long:", directive)
        self.assertIn("no extra generation", directive)
        self.assertIn("APPROVED_RESEARCH_PACK=EMPTY", directive)

    def test_source_derived_short_keeps_compact_atoms_from_long_source_only(self):
        narration = (
            "هذه جملة افتتاحية طويلة من الحلقة الأصلية تشرح الفكرة بهدوء ومن دون اختراع نص جديد. "
            "ثم تأتي جملة ختامية أخرى من المصدر نفسه لتثبيت المعنى للمشاهد."
        )
        on_screen = "واحد اثنان ثلاثة اربعة خمسة ستة سبعة ثمانية تسعة عشرة احد عشر اثنا عشر"
        key_point = "واحد اثنان ثلاثة اربعة خمسة ستة سبعة ثمانية تسعة عشرة احد عشر اثنا عشر ثلاثة عشر اربعة عشر خمسة عشر"
        excerpt = {
            "source_narration": narration,
            "source_narration_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
            "source_on_screen_text": on_screen,
            "source_key_point": key_point,
        }
        texts = _distinct_source_texts(excerpt)
        self.assertEqual(texts[0], " ".join(on_screen.split()[:10]))
        self.assertEqual(texts[1], " ".join(key_point.split()[:14]))
        self.assertLessEqual(len(texts[0].split()), craft.LONG_DERIVATIVE_ON_SCREEN_MAX_WORDS)
        self.assertLessEqual(len(texts[1].split()), craft.LONG_DERIVATIVE_KEY_POINT_MAX_WORDS)
        source_words = set((on_screen + " " + key_point + " " + narration).split())
        for text in texts:
            self.assertTrue(set(text.split()).issubset(source_words))

    def test_source_derived_short_never_hides_large_visual_gap_with_speed(self):
        with self.assertRaisesRegex(VoiceOwnedTimelineError, "source_safe_reprovision_required=true"):
            build_voice_owned_timeline(
                voice_seconds=18.0,
                source_visual_seconds=15.0,
                minimum_seconds=7.0,
                maximum_seconds=25.0,
                mode="voice_led",
                visible_beat_count=4,
                source_derived_from_long=True,
            )

    def test_source_derived_provisioning_is_only_a_media_budget_not_certification(self):
        seconds = provision_source_derived_visual_seconds([
            "هذا هو الخطاف الذي يفتح الفكرة",
            "ثم تتغير زاوية النظر قليلًا",
            "وفي النهاية تصل الفكرة بهدوء",
        ])
        self.assertGreaterEqual(seconds, 12.0)
        self.assertLessEqual(seconds, 24.5)

    def test_authoritative_short_port_routes_to_voice_owned_runtime_before_master_qc(self):
        source = inspect.getsource(port.prepare_authoritative_short_for_gold)
        voice_index = source.index("apply_voice_owned_short")
        qc_index = source.index("run_final_master_qc(output_dir)")
        self.assertLess(voice_index, qc_index)
        self.assertNotIn("apply_short_voice_v2", source)


if __name__ == "__main__":
    unittest.main()
