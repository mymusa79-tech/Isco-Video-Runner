from __future__ import annotations

import unittest

from scripts.qa_caption_sync_asr import compare, normalize

CARDS = [
    {"start": 0.0, "end": 2.0, "text": "تمرّ الفرشاة على الورق"},
    {"start": 2.0, "end": 4.0, "text": "متى يتحوّل الإتقان إلى فخ؟"},
]


def words(text, start, step=0.4):
    return [{"word": w, "start": start + i * step, "end": start + i * step + 0.3} for i, w in enumerate(text.split())]


class CaptionSyncQaTests(unittest.TestCase):
    def test_normalize_ignores_diacritics_and_alef_forms(self):
        self.assertEqual(normalize("إِلَى،"), normalize("الى"))
        self.assertEqual(normalize("تمرّ"), "تمر")

    def test_in_sync_cards_are_ok(self):
        heard = words("تمر الفرشاة على الورق", 0.05) + words("متى يتحول الاتقان الى فخ", 2.05)
        report = compare(CARDS, heard)
        self.assertEqual(report["status"], "measured")
        self.assertEqual([r["flag"] for r in report["cards"]], ["ok", "ok"])

    def test_card_shown_before_speech_is_flagged_early(self):
        heard = words("تمر الفرشاة على الورق", 0.05) + words("متى يتحول الاتقان الى فخ", 3.4)
        report = compare(CARDS, heard)
        self.assertEqual(report["cards"][1]["flag"], "early")
        self.assertLess(report["cards"][1]["start_error"], -1.0)

    def test_asr_substitution_still_matches_the_card(self):
        heard = words("تمر الفرشاة عالورق", 0.0) + words("متى يتحول الاتقان الى فخ", 2.0)
        report = compare(CARDS, heard)
        self.assertTrue(report["cards"][0]["matched"])

    def test_unrelated_speech_reports_insufficient_match(self):
        report = compare(CARDS, words("كلام مختلف تماما عن النص المكتوب هنا", 0.0))
        self.assertEqual(report["status"], "insufficient_match")

    def test_empty_input_is_no_data_not_a_crash(self):
        self.assertEqual(compare(CARDS, [])["status"], "no_data")


if __name__ == "__main__":
    unittest.main()
