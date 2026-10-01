from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2.narrative_history import (
    record_narrative_format,
    record_podcast_promo_signature,
    recent_narrative_formats,
    recent_podcast_promo_signatures,
)


class NarrativeHistoryTests(unittest.TestCase):
    def test_missing_file_returns_empty_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "narrative-history.json"
            self.assertEqual(recent_narrative_formats(path, "film"), ())

    def test_none_path_returns_empty_history_and_record_is_a_noop(self) -> None:
        self.assertEqual(recent_narrative_formats(None, "film"), ())
        record_narrative_format(None, "film", "question_answer")  # must not raise

    def test_record_then_read_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "narrative-history.json"
            record_narrative_format(path, "film", "question_answer")
            record_narrative_format(path, "film", "story_analysis")
            self.assertEqual(
                recent_narrative_formats(path, "film"),
                ("question_answer", "story_analysis"),
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["film"], ["question_answer", "story_analysis"])
            self.assertEqual(data["schema_version"], 1)

    def test_short_round_trip_is_independent_of_film(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            record_narrative_format(path, "film", "question_answer")
            record_narrative_format(path, "short", "inner_dialogue")
            record_narrative_format(path, "short", "why_reframe")
            self.assertEqual(recent_narrative_formats(path, "film"), ("question_answer",))
            self.assertEqual(recent_narrative_formats(path, "short"),
                             ("inner_dialogue", "why_reframe"))

    def test_podcast_promo_history_is_separate_from_fixed_podcast_narrative_style(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            record_narrative_format(path, "film", "question_answer")
            record_narrative_format(path, "short", "inner_dialogue")
            record_podcast_promo_signature(path, "problem:2:early")
            record_podcast_promo_signature(path, "reason:1:middle")
            self.assertEqual(
                recent_podcast_promo_signatures(path),
                ("problem:2:early", "reason:1:middle"),
            )
            self.assertEqual(recent_narrative_formats(path, "film"), ("question_answer",))
            self.assertEqual(recent_narrative_formats(path, "short"), ("inner_dialogue",))
            self.assertEqual(recent_narrative_formats(path, "podcast"), ())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["podcast_promo"], ["problem:2:early", "reason:1:middle"])

    def test_podcast_promo_history_keeps_its_own_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            for signature in ("a:1:early", "b:2:middle", "c:3:late"):
                record_podcast_promo_signature(path, signature, limit=2)
            self.assertEqual(
                recent_podcast_promo_signatures(path, limit=2),
                ("b:2:middle", "c:3:late"),
            )

    def test_only_the_most_recent_limit_entries_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "narrative-history.json"
            for name in ("a", "b", "c", "d", "e", "f"):
                record_narrative_format(path, "film", name, limit=5)
            self.assertEqual(
                recent_narrative_formats(path, "film"),
                ("b", "c", "d", "e", "f"),
            )

    def test_untracked_formats_are_ignored_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "narrative-history.json"
            record_narrative_format(path, "podcast", "dialogue_qa")
            self.assertEqual(recent_narrative_formats(path, "podcast"), ())
            # Nothing was ever written for an untracked format.
            self.assertFalse(path.is_file())

    def test_blank_or_missing_narrative_format_is_never_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "narrative-history.json"
            record_narrative_format(path, "film", "")
            record_narrative_format(path, "film", "   ")
            self.assertEqual(recent_narrative_formats(path, "film"), ())

    def test_corrupt_file_is_treated_as_empty_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "narrative-history.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(recent_narrative_formats(path, "film"), ())
            record_narrative_format(path, "film", "question_answer")
            self.assertEqual(
                recent_narrative_formats(path, "film"), ("question_answer",)
            )


if __name__ == "__main__":
    unittest.main()
