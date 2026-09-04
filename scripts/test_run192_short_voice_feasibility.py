from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import short_voice_v2
from scripts.short_voice_feasibility import (
    PREFLIGHT_SPEED_HEADROOM,
    RUNTIME_MAX_SPEED,
    build_voice_projection,
)


class Run192ShortVoiceFeasibilityTests(unittest.TestCase):
    def test_normal_voice_led_short_keeps_full_semantic_progression(self):
        events = [
            {"text": "تتردد للحظة"},
            {"text": "ثم تلاحظ السبب"},
            {"text": "فتغير الحركة الصغيرة"},
            {"text": "وتبدأ الآن"},
        ]
        result = build_voice_projection(events, "voice_led", final_seconds=15.0)
        self.assertEqual(result["strategy"], "full_semantic_progression")
        self.assertEqual(result["spoken_beat_count"], 4)
        self.assertEqual(result["omitted_beat_indexes"], [])
        self.assertTrue(result["on_screen_semantic_beats_preserved"])
        self.assertFalse(result["ai_rewrite_used"])

    def test_dense_voice_led_short_contracts_semantic_projection_before_tts(self):
        events = [
            {"text": "تظن أن الحماس يجب أن يسبق كل خطوة حتى تبدأ"},
            {"text": "لكن انتظار الشعور المثالي يحول البداية نفسها إلى عبء ثقيل"},
            {"text": "المفارقة أن الحركة الصغيرة هي التي تصنع الإحساس الذي كنت تنتظره"},
            {"text": "ابدأ بخطوة صغيرة الآن ودع الدافع يلحق بك"},
        ]
        result = build_voice_projection(events, "voice_led", final_seconds=15.0)
        self.assertEqual(result["strategy"], "bounded_semantic_projection")
        self.assertLess(result["spoken_beat_count"], result["original_beat_count"])
        self.assertEqual(result["spoken_beat_indexes"][0], 0)
        self.assertEqual(result["spoken_beat_indexes"][-1], 3)
        self.assertLessEqual(result["estimated_natural_seconds"], result["planning_budget_seconds"])
        self.assertEqual(result["runtime_max_speed_unchanged"], 1.20)

    def test_hybrid_keeps_hook_and_payoff_only(self):
        events = [
            {"text": "هذا هو السؤال"},
            {"text": "تفصيل داخلي"},
            {"text": "وهذه هي الخلاصة"},
        ]
        result = build_voice_projection(events, "hybrid", final_seconds=15.0)
        self.assertEqual(result["spoken_beat_indexes"], [0, 2])
        self.assertIn("هذا هو السؤال", result["transcript"])
        self.assertIn("وهذه هي الخلاصة", result["transcript"])
        self.assertNotIn("تفصيل داخلي", result["transcript"])

    def test_impossible_hook_payoff_budget_fails_before_provider_call(self):
        events = [
            {"text": " ".join(["مقدمة"] * 30)},
            {"text": " ".join(["خاتمة"] * 30)},
        ]
        with self.assertRaisesRegex(RuntimeError, "impossible without rewriting"):
            build_voice_projection(events, "voice_led", final_seconds=15.0)

    def test_runtime_speed_ceiling_remains_unchanged(self):
        self.assertEqual(RUNTIME_MAX_SPEED, 1.20)
        self.assertLess(PREFLIGHT_SPEED_HEADROOM, RUNTIME_MAX_SPEED)
        with tempfile.TemporaryDirectory() as td:
            voice_path = Path(td) / "voice.wav"
            voice_path.write_bytes(b"a" * 4096)
            with mock.patch.object(short_voice_v2, "duration", return_value=24.66):
                with self.assertRaisesRegex(RuntimeError, "speed_required=1.661"):
                    short_voice_v2._fit_voice_to_video(voice_path, 15.0)

    def test_apply_short_voice_builds_projection_before_tts(self):
        source = inspect.getsource(short_voice_v2.apply_short_voice_v2)
        projection_at = source.index("build_voice_projection(")
        synth_at = source.index("orchestrator._synthesize_tts_section(")
        fit_at = source.index("_fit_voice_to_video(")
        self.assertLess(projection_at, synth_at)
        self.assertLess(synth_at, fit_at)
        self.assertIn('"voice_duration_preflight": True', source)
        self.assertIn('"extra_text_ai_calls": 0', source)


if __name__ == "__main__":
    unittest.main()
