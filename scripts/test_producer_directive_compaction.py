from __future__ import annotations

import unittest

from scripts import producer_quality_contract as producer


class ProducerDirectiveCompactionTests(unittest.TestCase):
    def test_compaction_preserves_required_guidance_and_evidence_state(self) -> None:
        present = producer.producer_writing_directive(
            {"approved_research_pack": [{"claim": "approved"}]}
        )
        empty = producer.producer_writing_directive({})
        for directive in (present, empty):
            self.assertIn("APPROVED_RESEARCH_PACK", directive)
            self.assertIn("precise/high-risk", directive)
            self.assertIn("modest wording", directive)
            self.assertIn("Natural MSA", directive)
            self.assertIn("non-diagnostic", directive)
            self.assertIn("non-preachy", directive)
            self.assertIn("generic AI motivation", directive)
            self.assertIn("narrative/template", directive)
            self.assertIn("distinct beats", directive)
            self.assertIn("direct commands", directive)
            self.assertIn("on_screen_text", directive)
            self.assertIn("template progression", directive)
        self.assertIn("APPROVED_RESEARCH_PACK=present", present)
        self.assertIn("APPROVED_RESEARCH_PACK=EMPTY", empty)
        # Keep meaningful transport margin instead of merely fitting the current
        # boundary by a few tokens. Deterministic acceptance remains outside this prose.
        self.assertLess(len(present.encode("utf-8")), 360)
        self.assertTrue(callable(producer.validate_plan_for_producer_handoff))


if __name__ == "__main__":
    unittest.main()
