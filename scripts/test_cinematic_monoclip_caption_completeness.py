"""All spoken Monoclip words, including the final spoken payoff, become captions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.podcast_captions import build_events
from clean_v2.short_timed_text import (
    _clean, _cinematic_monoclip_caption_events, _require_monoclip_cairo_font,
    build_rich_ass, validate_progressive_text, write_progressive_srt,
)


class MonoclipCaptionCompleteness(unittest.TestCase):
    def test_monoclip_caption_cards_use_meaningful_phrases(self):
        for count in range(2, 41):
            with self.subTest(count=count):
                text = " ".join(f"كلمة{i}" for i in range(count))
                result = _cinematic_monoclip_caption_events([
                    {"start": 0.0, "end": float(count), "text": text}
                ])
                self.assertTrue(all(2 <= len(e["text"].split()) <= 8 for e in result))
                self.assertEqual(" ".join(e["text"] for e in result), text)
                self.assertAlmostEqual(float(result[-1]["end"]), float(count))

    def test_words_are_retained_with_voice_owned_natural_pauses(self):
        events = [
            {"start": 0.0, "end": 4.0, "text": "قد تمر بجانب أجمل المشاهد ولا تراها."},
            {"start": 4.7, "end": 8.3, "text": "لأن عقلك سبقك إلى آخر الطريق."},
            {"start": 8.7, "end": 12.2, "text": "أبطئ قليلًا وانظر إلى ما حولك."},
        ]
        result = _cinematic_monoclip_caption_events(events)
        self.assertEqual(
            " ".join(event["text"] for event in result),
            " ".join(event["text"] for event in events),
        )
        self.assertEqual(result[-1]["end"], 12.2)
        self.assertTrue(result[-1]["text"].endswith("حولك."))
        self.assertLessEqual(len(result), 6)
        self.assertTrue(all(event["end"] > event["start"] for event in result))

    def test_approved_big_cairo_text_keeps_two_rows_and_no_outro(self):
        from clean_v2.short_timed_text import _caption_rows_and_size
        text = "وتنسى أن تنظر إلى ما حولك."
        rows, size = _caption_rows_and_size(text, fmt="short", font_size=144)
        self.assertLessEqual(len(rows), 2)
        self.assertGreaterEqual(size, 120)
        self.assertEqual(" ".join(rows), text)

    def test_monoclip_rejects_other_font_substitutions(self):
        completed = mock.Mock(stdout="Noto Sans", stderr="")
        with mock.patch("clean_v2.short_timed_text.subprocess.run", return_value=completed):
            from clean_v2.short_timed_text import ShortTimedTextError
            with self.assertRaisesRegex(ShortTimedTextError, "cairo_bold_font_not_installed"):
                _require_monoclip_cairo_font()
        completed.stdout = "Cairo"
        with mock.patch("clean_v2.short_timed_text.subprocess.run", return_value=completed):
            _require_monoclip_cairo_font()

    def test_three_spoken_sections_keep_every_word_and_final_payoff(self):
        parts = [
            "قد تمشي وسط أجمل منظر... ولا تراه. كيف يحدث ذلك؟",
            "ننطلق أحيانًا ونحن نحسب ما بقي من الطريق. كم خطوة؟ كم دقيقة؟ وماذا بعد؟",
            "الطريق لا ينتظرك حتى تستمتع به. هذه اللحظة تمر مرة واحدة.",
        ]
        spans = [(0.0, 3.2, "hook"), (3.45, 13.0, "topic"), (13.0, 17.0, "topic")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline = {"status": "pass", "audio_units": [
                {
                    "section_id": f"s{i+1}", "chunk": 1, "role": role,
                    "start": start, "end": end,
                    "duration_seconds": round(end - start, 3),
                }
                for i, (start, end, role) in enumerate(spans)
            ]}
            report = {"sections": [
                {"id": f"s{i+1}", "chunks": [{"chunk": 1, "text": text}]}
                for i, text in enumerate(parts)
            ]}
            (root / "timeline-first.json").write_text(json.dumps(timeline, ensure_ascii=False))
            (root / "voice-sections.json").write_text(json.dumps(report, ensure_ascii=False))
            parsed, _timing = build_events(
                root, fmt="short", caption_roles={"hook", "topic", "outro"}
            )
            self.assertGreaterEqual(len(parsed), 3)
            normalized = _clean(" ".join(e["text"] for e in parsed))
            self.assertEqual(normalized, _clean(" ".join(parts)))
            self.assertEqual(parsed[-1]["end"], spans[-1][1])
            self.assertTrue(parsed[-1]["text"].endswith("واحدة."))
            mini = _cinematic_monoclip_caption_events(parsed)
            self.assertTrue(all(2 <= len(e["text"].split()) <= 8 for e in mini))
            self.assertEqual(_clean(" ".join(e["text"] for e in mini)), normalized)
            self.assertEqual(mini[-1]["end"], parsed[-1]["end"])
            self.assertTrue(mini[-1]["text"].endswith("واحدة."))
            self.assertGreater(len(mini), 3)  # Multiple voice-owned semantic cards
            styled = [
                dict(event, role="hook" if i == 0 else
                     ("payoff" if i == len(parsed)-1 else "beat"))
                for i, event in enumerate(parsed)
            ]
            validate_progressive_text(styled)
            for bw in (False, True):
                ass = build_rich_ass(styled, bw_cinematic=bw)
                self.assertIn("Cairo", ass)
                self.assertIn("Dialogue: 0", ass)
                if bw:
                    self.assertIn("&H00F5F5F5", ass)
            srt = write_progressive_srt(styled, root / "captions.srt")
            self.assertIn("هذه اللحظة", srt.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
