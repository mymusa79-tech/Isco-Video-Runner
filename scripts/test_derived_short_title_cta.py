from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from clean_v2.derived_short import _teaser_cta_ass  # noqa: E402
import clean_v2_release_delivery as delivery  # noqa: E402


class DerivedTitleTests(unittest.TestCase):
    def test_title_is_first_question(self) -> None:
        text = "لماذا يؤلمنا الانسحاب أمام الناس أكثر من الخسارة؟ لأن الناس تشاهد. وهذا مهم."
        self.assertEqual(delivery._derived_hook_title(text), "لماذا يؤلمنا الانسحاب أمام الناس أكثر من الخسارة؟")

    def test_falls_back_to_first_sentence_and_trims(self) -> None:
        text = "كلمة " * 40 + "."
        title = delivery._derived_hook_title(text)
        self.assertLessEqual(len(title), 63)
        self.assertTrue(title.endswith("…"))

    def test_empty_text(self) -> None:
        self.assertEqual(delivery._derived_hook_title(""), "")
        self.assertEqual(delivery._derived_hook_title(None), "")

    def test_generic_section_label_detected(self) -> None:
        self.assertTrue(delivery._GENERIC_SECTION_TITLE.match("الجزء 1"))
        self.assertFalse(delivery._GENERIC_SECTION_TITLE.match("لماذا نخاف؟"))


class MetadataTests(unittest.TestCase):
    def _meta(self, heading: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "plan.json").write_text(json.dumps({"sections": [{"id": "s1", "heading": heading}]}), encoding="utf-8")
            (root / "derived-short.json").write_text(json.dumps({
                "section_id": "s1", "text": "لماذا يؤلمنا الانسحاب أمام الناس؟ لأن الناس تشاهد."}), encoding="utf-8")
            return delivery._derived_short_publish_metadata(
                root, parent_kind="podcast", parent={"title": "الحلقة", "topic": "t"}, short_prefix="derived-short")

    def test_generic_heading_is_replaced_by_hook(self) -> None:
        self.assertEqual(self._meta("الجزء 1")["title"], "لماذا يؤلمنا الانسحاب أمام الناس؟ | خارج النص")

    def test_real_heading_is_kept(self) -> None:
        self.assertEqual(self._meta("الانسحاب")["title"], "الانسحاب | خارج النص")


class CtaCardTests(unittest.TestCase):
    def test_card_layers_and_window(self) -> None:
        ass = _teaser_cta_ass(30.0, podcast=True)
        for name in ("shade", "card", "rim", "accent", "line"):
            self.assertIn(f",Teaser,{name},", ass)
        self.assertIn("0:00:25.00,0:00:29.92", ass)
        self.assertIn("شاهد البودكاست", ass); self.assertIn("كاملًا", ass)
        self.assertIn("شاهد الحلقة", _teaser_cta_ass(30.0, podcast=False))

    def test_short_clip_starts_at_zero(self) -> None:
        self.assertIn("0:00:00.00,", _teaser_cta_ass(4.0, podcast=True))

    def test_card_is_large_with_a_gold_payoff_line(self) -> None:
        ass = _teaser_cta_ass(30.0, podcast=True)
        self.assertIn("Style: Teaser,Cairo,148,", ass)
        # Card 1000px wide, top below the Shorts header, white call then a gold payoff word.
        self.assertIn("شاهد البودكاست\\N{\\1c&H4BA2C9&}كاملًا", ass)
        self.assertIn("\\t(0,260,\\fscx100\\fscy100)", ass)

    def test_closing_chime_is_generated_not_downloaded(self) -> None:
        from clean_v2 import derived_short

        self.assertTrue(derived_short.CTA_CHIME_SOURCE.startswith("aevalsrc="))
        self.assertGreater(derived_short.CTA_CHIME_VOLUME, 0.5)
        self.assertNotIn("http", derived_short.CTA_CHIME_SOURCE)


class HookCardSizeTests(unittest.TestCase):
    def test_short_hook_is_one_big_line(self) -> None:
        from clean_v2.derived_short import _hook_ass

        ass = _hook_ass("هل التذكر حكم؟", 30.0)
        self.assertIn("Style: Hook,Cairo,130,", ass)
        self.assertNotIn("\\N", ass.split("[Events]")[1])

    def test_long_hook_breaks_into_two_balanced_lines_and_shrinks(self) -> None:
        from clean_v2.derived_short import _hook_ass

        medium = _hook_ass("ما الذي يفتحه الهدوء؟", 30.0)
        self.assertIn("Style: Hook,Cairo,116,", medium)
        self.assertEqual(medium.split("[Events]")[1].count("\\N"), 1)
        long_ = _hook_ass("لماذا نعيد أخطاءنا القديمة كل ليلة قبل النوم؟", 30.0)
        self.assertIn("Style: Hook,Cairo,94,", long_)


if __name__ == "__main__":
    unittest.main()
