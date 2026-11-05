"""All spoken Monoclip words, including the final spoken payoff, become captions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2.podcast_captions import build_events
from clean_v2.short_timed_text import (
    _clean, build_rich_ass, validate_progressive_text, write_progressive_srt,
)


class MonoclipCaptionCompleteness(unittest.TestCase):
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
