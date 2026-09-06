from __future__ import annotations

import hashlib
import inspect
import unittest

from scripts import short_editorial_craft_contract as craft
from scripts import short_voice_owned_timeline
from scripts import short_voice_v2
from scripts.source_derived_short_planner import _distinct_source_texts
from scripts.voice_owned_timeline import retime_events


class Run219F20LateBindingTests(unittest.TestCase):
    def test_finished_short_refresh_uses_live_module_binding(self) -> None:
        source = inspect.getsource(short_voice_owned_timeline.apply_voice_owned_short)
        self.assertIn("short_voice_v2._refresh_quality_final(root, final_path)", source)
        self.assertNotIn("quality = _refresh_quality_final(root, final_path)", source)

        original = short_voice_v2._refresh_quality_final
        sentinel = object()
        try:
            short_voice_v2._refresh_quality_final = sentinel  # type: ignore[assignment]
            self.assertIs(short_voice_owned_timeline.short_voice_v2._refresh_quality_final, sentinel)
        finally:
            short_voice_v2._refresh_quality_final = original


class ShortTemplateCraftTests(unittest.TestCase):
    def test_all_four_templates_have_distinct_opening_contracts(self) -> None:
        self.assertEqual(
            craft.template_names(),
            ("why_reframe", "inner_dialogue", "micro_story", "quote_reflection"),
        )
        caps = {
            name: craft.template_hook_beat_max_seconds(name)
            for name in craft.template_names()
        }
        self.assertEqual(caps["why_reframe"], 3.0)
        self.assertEqual(caps["inner_dialogue"], 3.2)
        self.assertEqual(caps["micro_story"], 3.3)
        self.assertEqual(caps["quote_reflection"], 3.5)
        self.assertLessEqual(craft.template_hook_word_limit("why_reframe"), 8)
        directive = craft.craft_writing_directive()
        for name in craft.template_names():
            self.assertIn(name, directive)
        self.assertIn("Long", directive)
        self.assertIn("no extra generation", directive)

    def test_run219_like_timeline_caps_first_beat_without_speed_or_text_change(self) -> None:
        events = [
            {"start": 0.0, "end": 2.8, "text": "hook"},
            {"start": 2.8, "end": 7.0, "text": "development"},
            {"start": 7.0, "end": 11.0, "text": "turn"},
            {"start": 11.0, "end": 14.0, "text": "payoff"},
        ]
        target = 20.97
        for template in craft.template_names():
            with self.subTest(template=template):
                cap = craft.template_hook_beat_max_seconds(template)
                retimed = retime_events(
                    events,
                    source_seconds=14.0,
                    target_seconds=target,
                    first_event_max_seconds=cap,
                )
                self.assertLessEqual(float(retimed[0]["end"]), cap + 0.001)
                self.assertAlmostEqual(float(retimed[-1]["end"]), target, places=3)
                self.assertEqual([item["text"] for item in retimed], [item["text"] for item in events])
                previous_end = 0.0
                for item in retimed:
                    self.assertGreaterEqual(float(item["start"]), previous_end - 0.001)
                    self.assertGreater(float(item["end"]), float(item["start"]))
                    previous_end = float(item["end"])


class LongToSiblingShortCraftTests(unittest.TestCase):
    def test_source_derived_short_uses_compact_exact_source_atoms(self) -> None:
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
        for text in texts:
            source_words = set((on_screen + " " + key_point + " " + narration).split())
            self.assertTrue(set(text.split()).issubset(source_words))


if __name__ == "__main__":
    unittest.main()
