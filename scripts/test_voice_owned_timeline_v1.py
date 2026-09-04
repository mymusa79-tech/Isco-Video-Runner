from __future__ import annotations

import inspect
import unittest

from scripts import orchestration_shorts_port as port
from scripts import short_voice_owned_timeline as runtime
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
