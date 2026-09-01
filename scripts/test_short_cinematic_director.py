from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from scripts import short_cinematic_director as director
from scripts import short_voice_v2


class ShortCinematicDirectorTests(unittest.TestCase):
    def test_each_semantic_beat_gets_one_bounded_shot(self):
        for beats, expected in ((2, 2), (3, 3), (4, 4), (5, 4)):
            events = [
                {"start": float(i), "end": float(i + 1), "text": f"beat {i}"}
                for i in range(beats)
            ]
            with self.subTest(beats=beats):
                self.assertEqual(director.required_shot_count(events, 15.0), expected)
        with self.assertRaisesRegex(director.ShortCinematicError, "at least two"):
            director.required_shot_count([{"start": 0, "end": 1}], 8.0)

    def test_all_four_templates_have_distinct_primary_and_alternate_visual_intents(self):
        for template in ("why_reframe", "inner_dialogue", "micro_story", "quote_reflection"):
            primary, alternate = director.beat_queries(
                "person walking through quiet city evening",
                template,
                2,
            )
            with self.subTest(template=template):
                self.assertNotEqual(primary, alternate)
                self.assertIn("portrait vertical realistic cinematic", primary)
                self.assertIn("portrait vertical realistic cinematic", alternate)
                self.assertLessEqual(len(primary), 260)
                self.assertLessEqual(len(alternate), 260)

    def test_visual_review_budget_is_two_semantic_calls_max_per_added_beat(self):
        self.assertEqual(director.MAX_VISION_REVIEWS_PER_ATTEMPT, 1)
        self.assertEqual(director.MAX_VISION_REVIEWS_PER_BEAT, 2)
        source = inspect.getsource(director.upgrade_short_cinematic)
        self.assertIn("max_candidates_per_attempt=MAX_VISION_REVIEWS_PER_ATTEMPT", source)
        self.assertIn('"max_vision_reviews_per_additional_beat": MAX_VISION_REVIEWS_PER_BEAT', source)

    def test_source_derived_short_is_not_replaced_with_unrelated_stock(self):
        pre = {"short_template": "micro_story", "compensation": {"source": "long"}}
        result = director.upgrade_short_cinematic(
            Path("."),
            {"kind": "short", "approval_scope": "short_sibling"},
            pre,
            ledger=object(),
        )
        self.assertIs(result, pre)

    def test_quote_reflection_intentionally_uses_no_generated_sfx(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pre = {
                "short_template": "quote_reflection",
                "timed_text_events": [
                    {"start": 0.0, "end": 4.0, "text": "quote"},
                    {"start": 4.0, "end": 8.0, "text": "payoff"},
                ],
            }
            result = director.apply_short_sfx(root, pre)
            self.assertIs(result, pre)
            report = (root / "short-sfx-plan.json").read_text(encoding="utf-8")
            self.assertIn('"status": "not_applicable"', report)
            self.assertIn('"max_accents_per_short": 1', report)

    def test_runtime_order_finishes_visual_audio_before_authoritative_quality_refresh(self):
        source = inspect.getsource(short_voice_v2.apply_short_voice_v2)
        voice_mix = source.index("_mix_voice(")
        director_at = source.index("upgrade_short_cinematic(")
        sfx_at = source.index("apply_short_sfx(")
        quality_at = source.index("_refresh_quality_final(")
        rights_at = source.index("_record_voice_rights(")
        self.assertLess(voice_mix, director_at)
        self.assertLess(director_at, sfx_at)
        self.assertLess(sfx_at, quality_at)
        self.assertLess(quality_at, rights_at)

    def test_director_source_requires_visual_qa_m8_rights_freshness_and_distinct_assets(self):
        source = inspect.getsource(director.upgrade_short_cinematic)
        self.assertIn("select_with_recovery(", source)
        self.assertIn("_stable_intent_audit", source)
        self.assertIn("_prepare_m8_clip(", source)
        self.assertIn("_append_rights(", source)
        self.assertIn("cache = VisualCandidateCache()", source)
        self.assertIn("recent_visual_history_exclusion", source)
        self.assertIn("distinct_asset_count", source)
        self.assertIn("hard_cut_default_for_short_retention", source)
        self.assertIn("SHORT_VISUAL_AUDIT", source)

    def test_visual_audit_provenance_keeps_editorial_intent_separate_from_retrieval_hint(self):
        source = inspect.getsource(director.upgrade_short_cinematic)
        self.assertIn('"intended_visual": primary_query', source)
        self.assertIn("selected_query = result.alternate_query if result.used_alternate_query else primary_query", source)
        self.assertIn('"query": selected_query', source)


if __name__ == "__main__":
    unittest.main()
