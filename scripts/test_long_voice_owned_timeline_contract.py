from __future__ import annotations

import inspect
import unittest

import isco_video_agent.orchestrator as orchestrator


class LongVoiceOwnedTimelineContractTests(unittest.TestCase):
    def test_long_form_keeps_measured_narration_as_visual_duration_input(self):
        source = inspect.getsource(orchestrator)
        self.assertIn("section_durations", source)
        self.assertIn("duration(", source)
        self.assertIn("sec_dur", source)

    def test_long_form_has_no_post_hoc_voice_atempo_contract(self):
        source = inspect.getsource(orchestrator)
        self.assertNotIn("atempo=", source)
        self.assertNotIn("speed_required", source)


if __name__ == "__main__":
    unittest.main()
