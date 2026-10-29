"""Film beats prepare clips from their own text span, not the whole section length."""
import unittest

from clean_v2 import owner_art


def _doc():
    text = "اولا كلمة كلمة كلمة كلمة. ثانيا كلمة كلمة كلمة كلمة كلمة كلمة كلمة كلمة."
    return {"script": {"sections": [{"id": "s1", "text": text}]}}


class FilmBeatSeconds(unittest.TestCase):
    def test_shares_follow_word_spans_and_stay_below_section_length(self):
        beats = [
            {"id": "s1_g1", "section_id": "s1", "writer_anchor_ar": "اولا كلمة"},
            {"id": "s1_g2", "section_id": "s1", "writer_anchor_ar": "ثانيا كلمة"},
        ]
        seconds = owner_art._span_beat_seconds(_doc(), beats, {"s1": 100})
        self.assertEqual(set(seconds), {"s1_g1", "s1_g2"})
        self.assertLess(seconds["s1_g1"], seconds["s1_g2"])
        self.assertLess(seconds["s1_g2"], 100)
        self.assertGreaterEqual(min(seconds.values()), 8.0)

    def test_unlocatable_anchor_keeps_legacy_estimate(self):
        beats = [{"id": "s1_g1", "section_id": "s1", "writer_anchor_ar": "غير موجود"}]
        self.assertEqual(owner_art._span_beat_seconds(_doc(), beats, {"s1": 100}), {})


if __name__ == "__main__":
    unittest.main()
