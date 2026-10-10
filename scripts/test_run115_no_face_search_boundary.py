from __future__ import annotations

import unittest

from clean_v2.visual_story import (
    _face_safe_stock_intent,
    _writer_searchable_intent,
    bind_visual_story_to_script,
)


class Run115NoFaceSearchBoundaryTests(unittest.TestCase):
    """Regression: stock searches must not ask for faces then block faces in QA."""

    def test_real_run115_rejected_query_keeps_action_but_removes_face_demand(self) -> None:
        authored = "person starting to write in a clean notebook with a focused expression"
        self.assertEqual(
            _writer_searchable_intent(authored),
            "hands starting to write in a clean notebook no face visible",
        )

    def test_notebook_face_safe_alternate_is_preserved_in_recovery(self) -> None:
        authored = "person holding a notebook with a pleased expression"
        self.assertEqual(
            _face_safe_stock_intent(authored),
            "hands holding a notebook no face visible",
        )

    def test_other_visual_meaning_not_rewritten(self) -> None:
        for intent in (
            "hands placing phone face down beside unfinished task",
            "shoes crossing doorway toward morning light no face",
            "closed notebook beside unfinished task hands only",
            "person writing in a notebook with a pen then looking at a desk",
        ):
            with self.subTest(intent=intent):
                self.assertEqual(_face_safe_stock_intent(intent), intent)

    def test_run115_voice_resume_refreshes_old_search_without_touching_proof(self) -> None:
        from clean_v2.visual_story import _face_safe_resumed_visual_story

        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                stale = {
                    "format": fmt,
                    "beats": [{
                        "id": "b5",
                        "shot_intent": "person starting to write in a clean notebook with a focused expression",
                        "stock_query_en": "person starting to write in a clean notebook with a focused expression",
                        "stock_query_alt_en": "person holding a notebook with a pleased expression",
                        "semantic_must_have": ["hands beginning to write", "face smiling"],
                        "writer_anchor_ar": "اتخذ خطوة صغيرة اليوم.",
                    }],
                }
                repaired, changed = _face_safe_resumed_visual_story(stale)
                self.assertTrue(changed)
                beat = repaired["beats"][0]
                self.assertEqual(
                    beat["stock_query_en"],
                    "hands starting to write in a clean notebook no face visible",
                )
                self.assertEqual(
                    beat["stock_query_alt_en"],
                    "hands holding a notebook no face visible",
                )
                self.assertEqual(beat["semantic_must_have"], stale["beats"][0]["semantic_must_have"])
                self.assertEqual(beat["writer_anchor_ar"], stale["beats"][0]["writer_anchor_ar"])
                self.assertEqual(stale["beats"][0]["shot_intent"], "person starting to write in a clean notebook with a focused expression")
                same, changed_again = _face_safe_resumed_visual_story(repaired)
                self.assertFalse(changed_again)
                self.assertEqual(same, repaired)

    def test_writer_binder_applies_same_boundary_across_all_formats(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                plan = {"format": fmt, "sections": [
                    {"id": "s1", "purpose": "Turn a setback into a practical next action"},
                ]}
                script = {"sections": [
                    {"id": "s1", "narration": "اكتب خطوة صغيرة قابلة للتطبيق."},
                ]}
                story = {"beats": [
                    {
                        "id": "b1",
                        "section_id": "s1",
                        "role": "body",
                        "shot_intent": "person starting to write in a clean notebook with a focused expression",
                        "stock_query_en": "person starting to write in a clean notebook with a focused expression",
                        "stock_query_alt_en": "person holding a notebook with a pleased expression",
                        "semantic_must_have": [
                            "hands writing in a notebook",
                            "clearly smiling face",
                        ],
                    },
                ]}
                bound = bind_visual_story_to_script(story, plan, script)
                beat = bound["beats"][0]
                self.assertEqual(
                    beat["shot_intent"],
                    "hands starting to write in a clean notebook no face visible",
                )
                self.assertEqual(beat["stock_query_en"], beat["shot_intent"])
                self.assertEqual(
                    beat["stock_query_alt_en"],
                    "hands holding a notebook no face visible",
                )
                self.assertEqual(beat["semantic_must_have"], ["hands writing in a notebook"])
                self.assertEqual(beat["writer_anchor_ar"], script["sections"][0]["narration"])
                self.assertIn("focused expression", story["beats"][0]["shot_intent"])


if __name__ == "__main__":
    unittest.main()
