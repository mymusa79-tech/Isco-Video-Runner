from __future__ import annotations

import unittest

from clean_v2.media import _reuse_existing_visual_competitor
from clean_v2.visual_qa import (
    _authored_recovery_visual_context,
    _recovery_source_preference,
)
from scripts.canonical_visual_evidence_v1 import canonical_visual_prompt


RUN116_ALTERNATE = (
    "hands dropping a pen in frustration while looking at a messy "
    "notebook filled with crossed-out notes"
)


def _story() -> dict:
    return {
        "beats": [
            {
                "id": "b1",
                "role": "hook",
                "shot_intent": "person sitting at a cluttered desk with crumpled papers",
                "stock_query_alt_en": RUN116_ALTERNATE,
                "meaning_target": "الشعور بالإخفاق بعد تجربة سلبية ثم بداية التعافي",
                "semantic_must_have": ["مكتب فوضوي وأوراق مبعثرة", "رسالة خطأ على حاسوب"],
                "semantic_should_avoid": ["recognizable faces", "advertising logos"],
            }
        ]
    }


class Run116AuthoredAlternateProofTests(unittest.TestCase):
    def test_authored_recovery_uses_alternative_visual_not_impossible_primary(self):
        story = _story()
        original = _authored_recovery_visual_context(
            story, "b1", RUN116_ALTERNATE, use_authored_proof=False
        )
        recovered = _authored_recovery_visual_context(
            story, "b1", RUN116_ALTERNATE, use_authored_proof=True
        )
        self.assertIn("Must show:مكتب فوضوي وأوراق مبعثرة", original)
        self.assertIn("Must show:" + RUN116_ALTERNATE, recovered)
        self.assertIn("Meaning:الشعور بالإخفاق", recovered)
        self.assertIn("Avoid:recognizable faces", recovered)
        self.assertNotIn("Must show:مكتب فوضوي", recovered)
        # Approved editorial input is not mutated by the temporary review context.
        self.assertEqual(story["beats"][0]["semantic_must_have"][0], "مكتب فوضوي وأوراق مبعثرة")

    def test_unapproved_runtime_rewrite_cannot_change_locked_proof(self):
        story = _story()
        result = _authored_recovery_visual_context(
            story, "b1", "person reading a book", use_authored_proof=True
        )
        self.assertIn("Must show:مكتب فوضوي وأوراق مبعثرة", result)
        self.assertNotIn("Must show:person reading a book", result)

    def test_reused_initial_candidate_is_judged_against_original_proof(self):
        prior = {
            "provider": "pexels", "asset_id": "p1", "download_url": "https://example.org/p1.mp4",
            "metadata_semantic_score": 0.5, "local_rank_score": 0.5,
        }
        ranked = [
            {"provider": "pixabay", "asset_id": "a", "metadata_semantic_score": 0.9, "local_rank_score": 0.9},
            {"provider": "pixabay", "asset_id": "b", "metadata_semantic_score": 0.8, "local_rank_score": 0.8},
        ]
        merged = _reuse_existing_visual_competitor(
            ranked, [prior], used_assets=set(), limit=3
        )
        self.assertEqual(merged[1]["recovery_shortlist_origin"], "initial_competitor")
        # Only fresh results for an authored alternate can use its proof;
        # prior original-query candidates never receive this privilege.
        use_authored = merged[1].get("recovery_shortlist_origin") != "initial_competitor"
        ctx = _authored_recovery_visual_context(
            _story(), "b1", RUN116_ALTERNATE, use_authored_proof=use_authored
        )
        self.assertIn("Must show:مكتب فوضوي وأوراق مبعثرة", ctx)

    def test_canonical_face_security_and_semantic_floor_remain_in_charge(self):
        ctx = _authored_recovery_visual_context(
            _story(), "b1", RUN116_ALTERNATE, use_authored_proof=True
        )
        prompt = canonical_visual_prompt(
            narration_context="يمكن أن نتعلم من الخطأ",
            intended_visual=ctx,
        )
        self.assertIn("NO-CLEAR-FACE POLICY", prompt)
        self.assertIn("PROOF missing/contradicted/uncertain requires status=block", prompt)
        self.assertIn(RUN116_ALTERNATE, prompt)

    def test_temporal_authored_alternate_uses_motion_for_recovery(self):
        beat = {"source_preference": "stock_still", "shot_intent": "open book on desk"}
        self.assertEqual(
            _recovery_source_preference(beat, "hands turning pages of a book"),
            "stock_motion",
        )
        self.assertEqual(
            _recovery_source_preference(beat, "open book lying on a table"),
            "stock_still",
        )


if __name__ == "__main__":
    unittest.main()
