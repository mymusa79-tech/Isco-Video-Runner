from __future__ import annotations

import inspect
import unittest

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.providers.gemini as gemini_provider


class LongVoiceOwnedTimelineContractTests(unittest.TestCase):
    def test_long_form_keeps_measured_narration_as_visual_duration_input(self):
        source = inspect.getsource(orchestrator)
        self.assertIn("section_durations", source)
        self.assertIn("d = duration(wav)", source)
        self.assertIn("audio_parts.append(wav); section_durations.append(d)", source)
        self.assertIn("for i, (sec, sec_dur) in enumerate(zip(plan.sections, section_durations), 1)", source)

    def test_long_form_has_no_post_hoc_voice_atempo_contract(self):
        source = inspect.getsource(orchestrator)
        self.assertNotIn("atempo=", source)
        self.assertNotIn("speed_required", source)

    def test_gemini_long_voice_preserves_human_pacing_and_breathing_room(self):
        source = inspect.getsource(gemini_provider.synthesize_wav)
        self.assertIn("Respect punctuation and meaningful pauses", source)
        self.assertIn("Do not race through sentence endings or compress silence merely to finish sooner", source)
        self.assertIn("Let a completed thought breathe before the next one", source)
        self.assertIn("recognizably human rather than announcer-like", source)
        self.assertIn("تحدث بسرعة طبيعية معتدلة", source)
        self.assertIn("ولا تبتلع نهايات الجمل", source)


if __name__ == "__main__":
    unittest.main()
