"""The Ala Mahl podcast must not show a generic blue slate before the outro."""
import unittest

from clean_v2 import media


def _schedule():
    return [
        {"start": 0.0, "end": 10.0, "duration_seconds": 10.0, "section_id": "s1", "path": "a.jpg"},
        {"start": 10.0, "end": 20.0, "duration_seconds": 10.0, "section_id": "s2", "path": "b.jpg"},
    ]


class TerminalSlotTests(unittest.TestCase):
    def test_podcast_last_slot_runs_to_timeline_end(self):
        schedule = _schedule()
        media._extend_last_slot_to_terminal(schedule, "ala_mahl_dialogue", 20.23)
        self.assertAlmostEqual(schedule[-1]["end"], 20.2333, places=3)
        self.assertAlmostEqual(schedule[-1]["duration_seconds"], 10.2333, places=3)
        self.assertEqual(schedule[0]["end"], 10.0)

    def test_short_template_behaviour_unchanged(self):
        schedule = _schedule()
        media._extend_last_slot_to_terminal(schedule, "ala_mahl", 21.0)
        self.assertEqual(schedule[-1]["end"], 21.0)

    def test_other_templates_are_untouched(self):
        for template in ("dialogue_qa", "hypothesis_test", ""):
            schedule = _schedule()
            media._extend_last_slot_to_terminal(schedule, template, 25.0)
            self.assertEqual(schedule[-1]["end"], 20.0, template)

    def test_empty_schedule_is_safe(self):
        media._extend_last_slot_to_terminal([], "ala_mahl_dialogue", 20.0)


if __name__ == "__main__":
    unittest.main()
