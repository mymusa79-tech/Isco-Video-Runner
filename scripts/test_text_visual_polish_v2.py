from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2 import short_timed_text as text_module
from clean_v2 import visual_cta as cta_module
from clean_v2 import podcast_key_text as podcast_text_module
from clean_v2.podcast_key_text import (
    FILM_MAX_EVENTS,
    FILM_MIN_GAP_SECONDS,
    build_ass as build_sparse_ass,
    build_events as build_sparse_events,
)


class TextVisualPolishV2Tests(unittest.TestCase):
    def test_arabic_phrase_keeps_logical_order_and_every_word(self) -> None:
        phrase = "جلست أمام القائمة الطويلة دون أن أشعر"
        wrapped = text_module._ass_wrap_words(phrase)
        restored = wrapped.replace(r"\N", " ").replace(text_module.ARABIC_WORD_GAP, " ")
        self.assertEqual(" ".join(restored.split()), phrase)
        self.assertNotIn("\u202B", wrapped)

        face = text_module._accent_caption(
            phrase,
            0,
            body_size=108,
            focus_size=132,
            role="hook",
        )
        self.assertIn(text_module.PRIMARY_ASS, face)
        self.assertIn(text_module.ACCENT_ASS, face)
        self.assertIn(r"\N", face)
        for word in phrase.split():
            self.assertIn(word, face)

        events = [
            {"start": 0.0, "end": 3.0, "text": phrase, "role": "hook"},
            {"start": 3.0, "end": 6.0, "text": "لكن الفكرة بدأت تتضح أمامي", "role": "beat"},
            {"start": 6.0, "end": 9.0, "text": "الخطوة الصغيرة تصنع الفرق", "role": "payoff"},
        ]
        ass = text_module.build_rich_ass(events)
        self.assertEqual(ass.count("Dialogue:"), len(events))
        self.assertIn("Style: Caption,Cairo", ass)
        self.assertIn(text_module.PRIMARY_ASS, ass)
        self.assertIn(r"\fad(140,200)", ass)
        self.assertNotIn("Style: Shadow", ass)
        self.assertNotIn("Style: Extrusion", ass)
        self.assertNotIn(r"\clip(", ass)
        self.assertNotIn(r"\t(0,", ass)

    def test_short_caption_is_static_cairo_bold_without_gold_wipe(self) -> None:
        events = [
            {"start": 0.0, "end": 4.0, "text": "أمسكت بالقلم فوق الصفحة الفارغة", "role": "hook"},
            {"start": 4.0, "end": 8.0, "text": "ثم بدأت الفكرة تتغير أمامي", "role": "beat"},
            {"start": 8.0, "end": 12.0, "text": "الخطوة الصغيرة فتحت الطريق", "role": "payoff"},
        ]
        ass = text_module.build_rich_ass(events)
        self.assertIn("Style: Caption,Cairo", ass)
        self.assertIn("أمسكت", ass)
        self.assertIn("الفارغة", ass)
        self.assertEqual(ass.count("Dialogue:"), len(events))
        self.assertNotIn(r"\kf", ass)
        self.assertNotIn(r"\clip(", ass)
        self.assertNotIn(r"\t(0,", ass)
        self.assertNotIn("Style: Extrusion", ass)
        self.assertNotIn("Style: Shadow", ass)

    def test_short_visual_text_comes_only_from_final_audited_script(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "سؤال المقارنة يبدأ من صورة ناقصة."},
                {"id": "s2", "narration": "نقطة البداية المختلفة تغيّر معنى النتيجة."},
                {"id": "s3", "narration": "التقدم الشخصي هو المقياس الأكثر صدقًا."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 8.0},
                {"section_id": "s2", "start": 8.0, "end": 13.0},
                {"section_id": "s3", "start": 13.0, "end": 18.0},
            ],
            "identity_events": [
                {"kind": "hook", "start": 0.0, "end": 3.0},
                {"kind": "topic", "start": 8.0, "end": 18.0},
            ],
        }
        events = text_module.build_events_from_voice_timeline(
            script=script,
            timeline_report=timeline,
        )
        text = " ".join(str(event["text"]) for event in events)
        self.assertIn("المقارنة", text)
        self.assertIn("البداية", text)
        self.assertIn("التقدم", text)
        self.assertFalse(hasattr(text_module, "_visual_asset_text_events"))

    def test_film_and_podcast_sparse_text_comes_only_from_final_script(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "توتر البداية الحقيقي يظهر عندما تقارن نتيجتك بغيرك."},
                {"id": "s2", "narration": "التحول يبدأ بفهم اختلاف الظروف ونقاط الانطلاق."},
                {"id": "s3", "narration": "النتيجة الأصدق هي قياس تقدمك من موضعك أنت."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 20.0},
                {"section_id": "s2", "start": 20.0, "end": 40.0},
                {"section_id": "s3", "start": 40.0, "end": 60.0},
            ],
        }
        podcast_events = build_sparse_events(
            script=script,
            timeline=timeline,
            fmt="podcast",
        )
        film_events = build_sparse_events(
            script=script,
            timeline=timeline,
            fmt="film",
        )
        combined = " ".join(
            str(event["text"]) for event in [*podcast_events, *film_events]
        )
        self.assertIn("البداية", combined)
        self.assertIn("النتيجة", combined)
        self.assertFalse(hasattr(podcast_text_module, "_visual_beat_text_events"))

    def test_short_renderer_disables_karaoke_and_keeps_voice_owned_event_timing(self) -> None:
        item = text_module.TimedTextEvent(
            start=1.0,
            end=5.0,
            text="ابدأ بخطوة صغيرة ثم واصل بهدوء",
            role="hook",
        )
        events = [
            {"start": 0.0, "end": 4.0, "text": item.text, "role": "hook"},
            {"start": 4.0, "end": 8.0, "text": "الفكرة تصبح أوضح عندما تبدأ فعلا", "role": "beat"},
            {"start": 8.0, "end": 12.0, "text": "الاستمرار الصغير يصنع الفرق", "role": "payoff"},
        ]
        ass = text_module.build_rich_ass(events)
        self.assertNotIn(r"\kf", ass)
        self.assertNotIn(r"\clip(", ass)
        self.assertNotIn(r"\t(0,", ass)
        self.assertEqual(ass.count("Dialogue:"), len(events))
        self.assertIn("Cairo", ass)
        self.assertIn(text_module.PRIMARY_ASS, ass)
        self.assertNotIn("\u202B", ass)

    def test_film_key_text_is_sparse_complete_and_breathes(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "البداية الحقيقية تبدأ عندما ترى المشكلة بوضوح."},
                {"id": "s2", "narration": "لكن أول قرار صغير يغير اتجاه يومك."},
                {"id": "s3", "narration": "وهنا يصبح التنفيذ أبسط مما توقعت."},
                {"id": "s4", "narration": "الحقيقة أن التقدم يظهر من التكرار الهادئ."},
                {"id": "s5", "narration": "وفي النهاية ترى نتيجة واضحة تستحق الاستمرار."},
            ]
        }
        timeline = {
            "status": "pass",
            "section_events": [
                {"section_id": "s1", "start": 0.0, "end": 60.0},
                {"section_id": "s2", "start": 60.0, "end": 120.0},
                {"section_id": "s3", "start": 120.0, "end": 180.0},
                {"section_id": "s4", "start": 180.0, "end": 240.0},
                {"section_id": "s5", "start": 240.0, "end": 300.0},
            ],
            "identity_events": [
                {"kind": "topic", "start": 8.0, "end": 294.0},
                {"kind": "outro", "start": 294.0, "end": 297.0},
            ],
        }
        events = build_sparse_events(script=script, timeline=timeline, fmt="film")
        self.assertGreaterEqual(len(events), 3)
        self.assertLessEqual(len(events), FILM_MAX_EVENTS)
        for left, right in zip(events, events[1:]):
            self.assertGreaterEqual(
                float(right["start"]) - float(left["end"]),
                FILM_MIN_GAP_SECONDS,
            )
        authored = " ".join(section["narration"] for section in script["sections"])
        for event in events:
            self.assertIn(str(event["text"]), authored)
            self.assertLessEqual(len(str(event["text"]).split()), 10)
        ass = build_sparse_ass(events, fmt="film")
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn(text_module.PRIMARY_ASS, ass)
        self.assertIn("Style: Caption,Cairo", ass)
        self.assertNotIn("Style: Shadow", ass)
        self.assertNotIn("Style: Extrusion", ass)

    def test_cta_does_not_repeat_writing_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {"section_events": [{"section_id": "s1", "start": 0.0, "end": 30.0}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "visual-story.json").write_text(
                json.dumps(
                    {
                        "beats": [
                            {
                                "section_id": "s1",
                                "viewer_intent": "يرى فعل الكتابة بوضوح",
                                "meaning_target": "شخص يكتب ملاحظة قصيرة",
                                "shot_intent": "hand writing in notebook with pen",
                                "semantic_must_have": ["writing", "notebook", "pen"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original = [
                cta_module.VisualCtaEvent(
                    mode="comment",
                    start_seconds=12.0,
                    end_seconds=13.4,
                    x=465,
                    y=cta_module.SHORT_CTA_Y,
                    asset="comment_ORIGINAL.png",
                )
            ]
            revised, decisions = cta_module._enforce_semantic_separation(
                events=original,
                output_dir=root,
                script={"sections": [{"id": "s1", "narration": "اكتب ملاحظة قصيرة الآن."}]},
                fmt="short",
            )
            self.assertNotEqual(revised[0].mode, "comment")
            self.assertIn(revised[0].mode, {"like", "share", "subscribe_combo"})
            self.assertLess(revised[0].x, 540)
            self.assertLess(revised[0].y, text_module.CAPTION_Y)
            self.assertTrue(decisions[0]["semantic_conflict_avoided"])


if __name__ == "__main__":
    unittest.main()
