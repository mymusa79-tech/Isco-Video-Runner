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


if __name__ == "__main__":
    unittest.main()
