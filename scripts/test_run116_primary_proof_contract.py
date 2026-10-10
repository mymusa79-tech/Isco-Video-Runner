from __future__ import annotations

import unittest

from clean_v2.visual_story import visual_review_context
from scripts.canonical_visual_evidence_v1 import canonical_visual_prompt


class Run116PrimaryProofContractTests(unittest.TestCase):
    def test_one_required_visual_proof_no_optional_promotion(self) -> None:
        beat = {
            "id": "b1", "role": "hook",
            "shot_intent": "cluttered desk with scattered paper and laptop",
            "meaning_target": "setback followed by a recoverable next step",
            "semantic_must_have": [
                "cluttered desk with scattered papers",
                "laptop showing an error message",
                "hands with a pen",
            ],
            "semantic_should_avoid": ["recognizable faces"],
        }
        context = visual_review_context({"beats": [beat]}, "b1", "fallback")
        prompt = canonical_visual_prompt(
            narration_context="The mistake is not the end.",
            intended_visual=context,
        )
        self.assertIn("Must show:cluttered desk with scattered papers", prompt)
        self.assertIn("Optional support (NOT required for PROOF: matched):", prompt)
        self.assertIn('ONLY the FIRST explicit "Must show:" cue is mandatory', prompt)
        self.assertIn("do NOT promote optional props", prompt)
        self.assertIn("do not report PROOF: missing solely because an optional prop is absent", prompt)
        self.assertIn("If the mandatory cue itself is absent", prompt)
        self.assertIn("NO-CLEAR-FACE POLICY", prompt)
        self.assertIn("CULTURAL & ISLAMIC SUITABILITY GATE", prompt)

    def test_no_contract_does_not_lose_strict_review(self) -> None:
        prompt = canonical_visual_prompt(
            narration_context="hands moving the book",
            intended_visual="pages visibly turning",
        )
        self.assertIn("When no Must show cue exists, apply the normal strict intended-meaning review", prompt)
        self.assertIn("A still may prove a static comparison, never an unseen motion", prompt)
        self.assertIn("PROOF missing/contradicted/uncertain requires status=block", prompt)


if __name__ == "__main__":
    unittest.main()
